# 15 — QEMU TCG Baseline, Measured on This Host

**Date:** 2026-08-08 · **Host:** Apple M2 Max (8P+4E), 32 GiB, macOS 26.5.2 (25F84), `hw.pagesize 16384`
**QEMU:** 11.0.3, Homebrew bottle `qemu 11.0.3` (`arm64_tahoe`), installed for this measurement
**Guest:** Alpine Linux 3.24.1, `vmlinuz-virt` + a custom initramfs, identical userspace on both ISAs
**Scripts / raw data:** `probes/qemu_baseline/` (`build.sh`, `runvm.py`, `sweep*.sh`, `profile.sh`, `results/`)

This document exists because `13-audit-skeptical-review.md` B-5 found that **every speedup claim in
the corpus is quoted against a QEMU TCG number nobody had measured on this machine**. The figures
came from papers using 2016–2019 QEMU vintages, on x86 hosts, and were then multiplied together.
This is the replacement denominator: measured here, on this silicon, with a stated control.

> **Read the denominator, not the multiplier.** Every ratio below names its numerator, its
> denominator and its run count. The headline control is *not* "vs native" — it is **vs the same
> QEMU, same guest OS, same benchmark source, running under `-accel hvf`**. That subtraction is
> what separates *emulation* cost from *virtualization + device-model* cost, and without it the
> TCG number is uninterpretable.

---

## 1. Bottom line

### 1.1 The number to quote

| Comparison | Geomean slowdown | n | Meaning |
|---|---|---|---|
| **x86-64 guest, `-accel tcg` ÷ aarch64 guest, `-accel hvf`** | **8.5×** (7.1× excluding the SIMD workload) | 3 runs × 10 workloads | **The baseline. Pure emulation cost.** Same QEMU binary version, same Alpine, same C source, same compiler. |
| x86-64 TCG ÷ bare macOS/arm64 | 8.9× (7.4× no-SIMD) | 3 × 10 | Conflates emulation with virtualization; quoted because the literature quotes it |
| **aarch64 guest HVF ÷ bare macOS/arm64** | **1.05×** | 3 × 10 | **Control.** Virtualization + guest-Linux + QEMU device model costs ~5%. So ~95% of the 8.5× above is emulation, not VM overhead. |
| **aarch64 guest TCG ÷ aarch64 guest HVF** | **4.2×** | 3 × 10 | Control. **Zero ISA mismatch** and TCG still loses 4.2×. Most of QEMU's deficit is "TCG is a simple JIT with a software MMU", not "x86 is hard". |
| **Rosetta 2 (x86-64 userspace) ÷ bare macOS/arm64** | **1.43×** | 3 × 10 | The practical ceiling for x86→arm64 translation on this silicon. |
| **x86-64 TCG ÷ Rosetta 2** | **6.2×** | 3 × 10 | How much headroom a *production-quality* x86→arm64 translator has over TCG — measured, not cited. |

### 1.2 Per-workload (medians of n=3; guest-reported time, cross-checked against host wall clock)

Denominator for the "slowdown" column is the **aarch64 HVF guest**.

| Workload | What it stresses | TCG x86-64 (s) | HVF aarch64 (s) | **slowdown** | cv (TCG) | Rosetta 2 ÷ native |
|---|---|---|---|---|---|---|
| `alu` | ILP=1 integer chain (latency-bound) | 0.900 | 0.397 | **2.27×** | 0.6% | 1.15× |
| `alu4` | ILP=4 integer, no memory ops | 0.921 | 0.426 | **2.16×** | 0.2% | 1.39× |
| `ls4` | `alu4` + 4 loads + 4 stores/iter, 4 KiB working set | 2.784 | 0.441 | **6.32×** | 0.4% | 1.28× |
| `ls4tlb` | same, but 8192-page address footprint | 35.53 | 1.135 | **31.3×** ⚠ | 5.4% | 1.88× |
| `ptr8` | pointer chase, 8 MiB | 2.902 | 0.261 | 11.1× ⚠ | **13.6%** | 1.03× |
| `ptr256` | pointer chase, 256 MiB (DRAM-latency-bound) | 17.60 | 1.396 | **12.6×** | 1.2% | 1.12× |
| `stream` | sequential 64-bit sum, 128 MiB × 100 | 3.313 | 0.242 | **13.7×** | 0.5% | 1.24× |
| `matmul` | 320³ int32 — **auto-vectorized**, see §6 | 8.005 | 0.190 | **42.1×** | 0.7% | 2.89× |
| `nqueens` | branchy backtracking, tiny footprint | 1.716 | 0.301 | **5.70×** | 1.5% | 1.24× |
| `syscall` | `getppid()` loop — guest user↔kernel | 1.442 | 0.344 | **4.20×** | 0.3% | 1.78× |

⚠ = see §7 for why this row's conclusion is less safe than the others.

### 1.3 Boot to a shell prompt (initramfs only — no disk I/O, so this is a CPU number)

Host wall clock from `exec qemu-…` to the guest printing `NATEMU_SHELL_UP` from a busybox shell.

| Configuration | median | min | max | n | cv |
|---|---|---|---|---|---|
| aarch64 guest, `-accel hvf` | **0.157 s** | 0.150 | 0.159 | 5 | 2.2% |
| aarch64 guest, `-accel tcg` | 1.295 s | 1.280 | 1.378 | 5 | 2.9% |
| **x86-64 guest, `-accel tcg`** | **2.238 s** | 2.227 | 2.328 | 5 | 2.0% |

x86-64 TCG boot is **14.3× slower** than the HVF control. Boot is more translation-heavy than
steady state (see §4), which is why it is worse than the 8.5× steady-state geomean.

---

## 2. Methodology

### 2.1 Why this shape of experiment

The naive experiment ("boot Debian under TCG, run 7-zip, divide by native") produces a number that
mixes at least five different costs and cannot inform any design decision. This one is built as an
**ablation with controls**:

* **Same-QEMU control (`-accel hvf`).** The aarch64 guest runs the *same QEMU 11.0.3*, the same
  Alpine 3.24.1 release, the same `virt` machine model, the same virtio-free initramfs boot, and a
  benchmark binary compiled from the *same C file by the same compiler*. Everything except the
  execution engine is held constant. Anything left is emulation.
* **Same-ISA control (aarch64 guest under TCG).** ARM-on-ARM TCG has no ISA mismatch at all. The
  gap between "aarch64 TCG ÷ HVF" (4.2×) and "x86-64 TCG ÷ HVF" (8.5×) is the part of QEMU's deficit
  that is genuinely attributable to *x86-ness* (flags, addressing modes, SIMD, segment/paging model).
* **Memory-op ablation (`alu4` / `ls4` / `ls4tlb`).** Three loops generated from one C macro, so the
  ALU work and index arithmetic are provably identical and the *only* difference is the presence of
  a guest load and a guest store. `t(ls4) − t(alu4)` divided by the exact memory-op count is the
  marginal cost of one guest memory access, measured in vivo. This is the measurement that decides
  the host-MMU question, and it is immune to the objection raised in audit B-6 against the earlier
  9-instruction microbenchmark, because it is measured inside a real compiled loop with real
  register pressure, real I-cache footprint and real branch prediction.
* **Translation-quality ceiling (Rosetta 2).** Same x86-64 machine code, same host, translated by a
  shipping production translator with hardware TSO and no software MMU.

### 2.2 Exact software

```
macOS            26.5.2 (25F84), Darwin 25.5.0, xnu-12377.121.10~1 RELEASE_ARM64_T6020
CPU              Apple M2 Max — hw.perflevel0.logicalcpu=8 (P), hw.perflevel1.logicalcpu=4 (E)
QEMU             11.0.3   (brew install qemu; bottle qemu--11.0.3.arm64_tahoe)
                 accelerators in qemu-system-x86_64: tcg only (hvf is aarch64-only on this host)
                 TCG plugins: supported (contrib plugins not shipped; one was built here)
zig (compiler)   0.16.0   — used as `zig cc` for all four targets
```

Guest images (all downloaded by `probes/qemu_baseline/build.sh`, SHA-256 verified):

```
alpine-minirootfs-3.24.1-x86_64.tar.gz   41f73e3cf5fa919b8aa5ca6b30dc48f0da2720776d7423e2a7748211456fe081
alpine-minirootfs-3.24.1-aarch64.tar.gz  f55a90f69052c5bd6f92cb09a8f47065970830b194c917a006fb94028e721259
alpine-netboot-3.24.1-x86_64.tar.gz      9a7769ea8fa1737b1b49d82f1bdd53d0a17338d6d3b7cfc6f2c3ec5158596d8b
alpine-netboot-3.24.1-aarch64.tar.gz     54fe38fa41cce740ba379458ed63cfcd89ab06ae5e6a47a06dafe0a34e8427e8
vmlinuz-virt x86_64  (from netboot)      1e6bf9027720c75c3ed0d79171f21b5791ee40ca9795d07c7c6e04dc5ea2ae90
vmlinuz-virt aarch64 (from netboot)      47970e0ee0478fe5c60824a89f162d5a353fa29466e5d3bddb0f9c506f1ed756
7zip-26.01-r0.apk (x86_64 / aarch64), 7z2601-mac.tar.xz (universal Mach-O)
```

The initramfs is the Alpine minirootfs plus `/bench` (statically linked, musl) plus `/7zz`, with
`/init` = `probes/qemu_baseline/guest_init.sh`. There is **no disk, no virtio, no network** — the
whole guest lives in RAM, so nothing measured here is I/O-bound.

### 2.3 The exact commands

```sh
# x86-64 full-system TCG (the numerator)
qemu-system-x86_64 -machine q35 -accel tcg,thread=single -cpu max -m 3072 -smp 1 \
  -kernel work/nb-x86_64/boot/vmlinuz-virt -initrd work/initramfs-x86_64.cpio \
  -append "console=ttyS0 natemu.task=bench panic=-1 rdinit=/init loglevel=3" \
  -display none -serial stdio -no-reboot -nodefaults -monitor none

# aarch64 full-system HVF (the denominator)
qemu-system-aarch64 -machine virt,gic-version=3 -accel hvf -cpu host -m 3072 -smp 1 \
  -kernel work/nb-aarch64/boot/vmlinuz-virt -initrd work/initramfs-aarch64.cpio \
  -append "console=ttyAMA0 natemu.task=bench panic=-1 rdinit=/init loglevel=3" \
  -display none -serial stdio -no-reboot -nodefaults -monitor none
```

Driver: `probes/qemu_baseline/runvm.py` launches QEMU and timestamps **every serial line with the
host monotonic clock**, so guest-reported times can be validated against host wall time. They agree
to within 1.5% on every workload (e.g. TCG `alu` guest 0.8801 s vs host-side delta 0.8940 s), which
also confirms the emulated TSC is not lying about time.

Benchmark source: `probes/qemu_baseline/bench.c`, one file, compiled four ways with identical flags
(`-O2 -fno-unroll-loops -g0`):

```
zig cc -target aarch64-macos      …        # bare macOS arm64
zig cc -target x86_64-macos       …        # Rosetta 2
zig cc -target aarch64-linux-musl … -static # aarch64 guest
zig cc -target x86_64-linux-musl  … -static # x86-64 guest
```

All iteration counts are compile-time constants: every environment performs bit-identical work, and
the checksums printed by each run match across all four environments (verified — see
`results/*.txt`, `chk=` fields).

### 2.4 Repetition, variance and machine state

* Every configuration was run **3×** (boot timings 5×); tables report the **median** and the
  coefficient of variation over the runs.
* `pmset -g therm` was sampled before every measurement and reported **no thermal warning level, no
  performance warning level, no CPU power status** throughout — i.e. no recorded DVFS throttling.
  This machine has 32 GiB and was otherwise idle apart from the user's normal desktop session
  (`vm.loadavg` ≈ 2.8 at start, which is a real caveat, see §7).
* **Core type.** `powermetrics` and `dtrace` need a password on this machine and were not available,
  so P/E residency could not be sampled directly. Instead an **empirical E-core control** was run:
  `taskpolicy -b` (background QoS, which macOS schedules onto E cores) gives `alu` 1.251 s vs
  0.389 s at default QoS — a **3.2× marker**. Every QEMU and native measurement reported here sits
  at the P-core level (`alu` ≈ 0.39–0.42 s in all non-TCG environments), so all work landed on P
  cores. Any run that had drifted to an E core would be obvious.
* **A first sweep was discarded.** During its second half, package downloads were running
  concurrently; `ptr8` showed cv=50%. Everything in this document comes from a re-run with the
  machine otherwise quiet. The discarded data is kept in `results/dirty/` and agrees with the clean
  data to within 3% on every workload except `ptr8`.

---

## 3. The measurement that matters: what does one guest memory access cost?

`alu4` and `ls4` are the same loop. `ls4` adds exactly **1 load + 1 store per chain × 4 chains ×
3×10⁸ iterations = 2.4×10⁹ guest memory accesses**, all to a single 4 KiB page (so: always L1-
resident, always TLB-resident — the *best case* for a software MMU).

| Environment | `alu4` (s) | `ls4` (s) | Δ (s) | **ns per guest memory access** |
|---|---|---|---|---|
| bare macOS arm64 | 0.4004 | 0.4442 | 0.0438 | 0.018 |
| Rosetta 2 | 0.5551 | 0.5688 | 0.0137 | 0.006 |
| aarch64 guest, HVF | 0.4255 | 0.4406 | 0.0151 | 0.006 |
| **x86-64 guest, TCG** | **0.9211** | **2.7835** | **1.8624** | **0.776** |
| aarch64 guest, TCG | 0.9674 | 2.1553 | 1.1879 | 0.495 |

**QEMU's software MMU costs 0.77 ns ≈ 2.7 host cycles per guest memory access on its fast path**
(M2 Max P-core ≈ 3.5 GHz). On hardware — whether bare, Rosetta-translated, or HVF-virtualized — the
same accesses are free (they hide in spare load/store issue slots).

That is the *fast* path. When the address footprint grows past what QEMU's software TLB holds:

| Environment | ns per `ls4` iteration (1 page) | ns per `ls4tlb` iteration (8192 pages) | ratio |
|---|---|---|---|
| bare macOS arm64 | 1.48 | 18.92 | 12.8× |
| aarch64 guest, HVF | 1.47 | 18.92 | 12.9× |
| Rosetta 2 | 1.90 | 35.64 | 18.8× |
| **x86-64 guest, TCG** | **9.28** | **592.2** | **63.8×** |
| aarch64 guest, TCG | 7.18 | 38.74 | 5.4× |

Each `ls4tlb` iteration is 8 memory accesses, so the marginal cost of a *TLB-missing* guest access
under x86-64 TCG is **(592.2 − 9.28)/8 ≈ 73 ns ≈ 255 host cycles**. The host profile (§4) confirms
this is a software page-table walk, not a cache miss: hardware pays 2.2 ns for the same access.

**Note the aarch64-TCG row.** ARM-on-ARM TCG pays only 38.7 ns/iteration for the same address
footprint, ~15× less than x86-on-ARM. The x86 software page-table walk in QEMU
(`target/i386/tcg/sysemu/excp_helper.c` → `mmu_translate`/`ptw_translate`) is dramatically more
expensive than the ARM one. This is an x86-specific cost that generic "TCG is slow" reasoning misses.

---

## 4. Where the host time actually goes

`sample` (macOS, 1 ms, 12 s window, no sudo needed) on the QEMU vCPU thread. With
`-accel tcg,thread=single` every other QEMU thread is blocked in a syscall for the whole window, so
this one thread *is* the process's CPU time. TCG-generated code has no symbol and no Mach-O image,
so it appears as `??? (in <unknown binary>)` — that bucket is exactly "executing translated code",
**including the inlined softmmu fast path**, because QEMU emits the fast path into the TB itself.
Parser: `probes/qemu_baseline/analyze_profile.py`.

| Workload | translated code | softmmu slow path<br>(TLB refill + SW page walk) | translation/<br>codegen | TB dispatch/<br>lookup | BQL / host kernel | unclassified |
|---|---|---|---|---|---|---|
| `alu4` (no memory ops) | **93.3%** | 0.5% | ~0% | 0.8% | 4.1% | 1.2% |
| `ls4` (L1-resident memory) | **93.9%** | 0.5% | ~0% | 0.7% | 3.9% | 0.9% |
| `stream` (sequential, 128 MiB) | 80.9% | 11.8% | ~0% | 0.8% | 4.4% | ~2% |
| `nqueens` (branchy) | 69.3% | 3.0% | ~0% | **20.2%** | 3.9% | 3.5% |
| `ptr256` (random, 256 MiB) | 7.8% | **83.3%** | ~0% | 0.7% | 3.1% | 4.9% |
| `ls4tlb` (8192-page footprint) | **2.4%** | **88.6%** | ~0% | 0.5% | 1.1% | 7.4% |
| boot (translation-heavy) | 35.4% | 18.9% | **12.7%** | 11.7% | 10.1% | 10.2% |

Four things fall out of this table:

1. **The softmmu fast path is invisible to a profiler and expensive anyway.** `ls4` spends 93.9% in
   "translated code" — but §3 shows 67% of `ls4`'s *time* is the memory ops. The fast path is
   inlined, so a host profile attributes it to the TB. Anyone who profiles QEMU and concludes
   "the MMU is only 0.5% of the time" has been fooled by exactly this. **The ablation, not the
   profile, is the correct instrument for the fast path.**
2. **The slow path is catastrophic and it is the common case for real address footprints.** At an
   8192-page footprint QEMU spends **88.6%** of host cycles in `probe_access_internal`,
   `ptw_translate`, `mmu_translate` and dirty-bitmap scanning, and **2.4%** actually running the
   guest program. The residual "unclassified" is also mostly the same code path
   (`qemu_ram_ptr_length`, `target_info`, `cpu_watchpoint_address_matches`).
3. **Branchy code pays a fifth of its time in TB dispatch.** `nqueens` (recursive backtracking →
   lots of indirect returns) spends **20.2%** in `cpu_tb_exec`/`tb_lookup`/`helper_lookup_tb_ptr`,
   versus 0.8% for the straight-line loops. TB chaining does not cover returns.
4. **~4% goes to the BQL even in a pure-compute guest.** In `alu4` the vCPU thread is blocked in
   `__psynch_mutexwait`/`__psynch_mutexdrop` for 4.1% of samples, contending with the main loop
   thread for the big QEMU lock. This is pure QEMU-architecture overhead with no analogue in a
   from-scratch design.

### 4.1 QEMU's own instrumentation

`--enable-profiler` no longer exists in QEMU 11 (the TCG profiler was removed), but the monitor's
`info jit` does, and it answers the code-expansion question directly. After a boot-only run:

```
gen code size       58565256/1073724416
TB count            88965
TB avg target size  20 bytes  (max 819)
TB avg host size    316 bytes      →  expansion ratio: 15.7
cross page TB count 285 (0%)
direct jump count   52913 (59%)  (2 jumps = 42571, 47%)
TB flush count      0
TB invalidate count 17238
TLB full flushes    0
TLB partial flushes 40977
TLB elided flushes  297007
```

**Measured x86-64 → arm64 code expansion under QEMU 11.0.3 is 15.7× (316 host bytes per 20 guest
bytes).** `12` rank 3 cites 17.4 from the literature; on this host, with this QEMU, it is 15.7. The
cited figure is close enough to be usable, and it is now checked rather than assumed.

Also worth noting: **only 59% of TBs get a direct jump**, so 41% return to the dispatch loop — which
is what §4 item 3 shows up as on branchy code.

---

<!--PLACEHOLDER-MTTCG-->

---

<!--PLACEHOLDER-7ZIP-->

---

<!--PLACEHOLDER-DOC12-->

---

<!--PLACEHOLDER-THREATS-->
