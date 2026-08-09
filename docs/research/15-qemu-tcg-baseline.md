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
| **x86-64 guest, `-accel tcg` ÷ aarch64 guest, `-accel hvf`** | **8.6×** (7.2× excluding the SIMD workload) | 3 runs × 10 workloads | **The baseline. Pure emulation cost.** Same QEMU binary version, same Alpine, same C source, same compiler. |
| x86-64 TCG ÷ bare macOS/arm64 | 9.0× (7.5× no-SIMD) | 3 × 10 | Conflates emulation with virtualization; quoted because the literature quotes it |
| **aarch64 guest HVF ÷ bare macOS/arm64** | **1.05×** | 3 × 10 | **Control.** Virtualization + guest-Linux + QEMU device model costs ~5%. So ~95% of the 8.6× above is emulation, not VM overhead. |
| **aarch64 guest TCG ÷ aarch64 guest HVF** | **4.2×** | 3 × 10 | Control. **Zero ISA mismatch** and TCG still loses 4.2×. Most of QEMU's deficit is "TCG is a simple JIT with a software MMU", not "x86 is hard". |
| **Rosetta 2 (x86-64 userspace) ÷ bare macOS/arm64** | **1.43×** | 3 × 10 | The practical ceiling for x86→arm64 translation on this silicon. |
| **x86-64 TCG ÷ Rosetta 2** | **6.3×** | 3 × 10 | How much headroom a *production-quality* x86→arm64 translator has over TCG — measured, not cited. |
| **QEMU 11 *user-mode* x86-64 ÷ aarch64, both inside the same HVF guest** | **3.5×** (2.7× excl. SIMD) | 3 × 10 | Ablation: same TCG code generator, **no software MMU**. See §5 — this is how the codegen share and the MMU share get separated without double-counting. |

### 1.2 Per-workload (medians of n=3; guest-reported time, cross-checked against host wall clock)

Denominator for the "slowdown" column is the **aarch64 HVF guest**.

| Workload | What it stresses | TCG x86-64 (s) | HVF aarch64 (s) | **slowdown** | cv (TCG) | Rosetta 2 ÷ native |
|---|---|---|---|---|---|---|
| `alu` | ILP=1 integer chain (latency-bound) | 0.900 | 0.397 | **2.27×** | 0.6% | 1.15× |
| `alu4` | ILP=4 integer, no memory ops | 0.921 | 0.426 | **2.16×** | 0.2% | 1.39× |
| `ls4` | `alu4` + 4 loads + 4 stores/iter, 4 KiB working set | 2.784 | 0.441 | **6.32×** | 0.4% | 1.28× |
| `ls4tlb` | same, but 8192-page address footprint | 35.53 | 1.135 | **31.3×** ⚠ | 5.4% | 1.88× |
| `ptr8` | pointer chase, 8 MiB | 3.351 † | 0.261 | **12.8×** | 4.5% (n=5) | 1.03× |
| `ptr256` | pointer chase, 256 MiB (DRAM-latency-bound) | 17.60 | 1.396 | **12.6×** | 1.2% | 1.12× |
| `stream` | sequential 64-bit sum, 128 MiB × 100 | 3.313 | 0.242 | **13.7×** | 0.5% | 1.24× |
| `matmul` | 320³ int32 — **auto-vectorized**, see §9 ⚠ | 8.005 | 0.190 | **42.1×** | 0.7% | 2.89× |
| `nqueens` | branchy backtracking, tiny footprint | 1.716 | 0.301 | **5.70×** | 1.5% | 1.24× |
| `syscall` | `getppid()` loop — guest user↔kernel | 1.442 | 0.344 | **4.20×** | 0.3% | 1.78× |

⚠ = see §9 (T-1, T-2) for why this row's conclusion is less safe than the others.
† `ptr8` is reported from **isolated fresh-VM runs** (n=5), not from the full-suite run, because it
is order-dependent inside QEMU: see §5.3, which is itself one of the more useful findings here.

### 1.3 Two whole-application numbers, for orientation

Both against the **aarch64 HVF** control, n=3:

| Application | x86-64 TCG | aarch64 HVF | slowdown |
|---|---|---|---|
| **7-Zip 26.01** `7zz b -mmt1 -md=22` (§7) | 893 MIPS compress / 1486 decompress | 9629 / 5445 | **10.8× / 3.66×** (6.4× total) |
| **400 × `fork`+`exec`** (§7.2) | 1.240 s | 0.040 s | **≈31×** (27–35×, see §7.2) |

The 25× spread between "LZMA decompression" and "process creation" *on the same emulator, same
host, same day* is the whole reason a single "QEMU is N× slower" number is not usable on its own.

### 1.4 Boot to a shell prompt (initramfs only — no disk I/O, so this is a CPU number)

Host wall clock from `exec qemu-…` to the guest printing `NATEMU_SHELL_UP` from a busybox shell.

| Configuration | median | min | max | n | cv |
|---|---|---|---|---|---|
| aarch64 guest, `-accel hvf` | **0.157 s** | 0.150 | 0.159 | 5 | 2.2% |
| aarch64 guest, `-accel tcg` | 1.295 s | 1.280 | 1.378 | 5 | 2.9% |
| **x86-64 guest, `-accel tcg`** | **2.238 s** | 2.227 | 2.328 | 5 | 2.0% |

x86-64 TCG boot is **14.3× slower** than the HVF control. Boot is more translation-heavy than
steady state (see §4), which is why it is worse than the 8.6× steady-state geomean.

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
  gap between "aarch64 TCG ÷ HVF" (4.2×) and "x86-64 TCG ÷ HVF" (8.6×) is the part of QEMU's deficit
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
  (`vm.loadavg` ≈ 2.8 at start, which is a real caveat, see §9 T-5).
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

| Workload | translated<br>code | softmmu slow path<br>(TLB refill + SW walk) | translation /<br>codegen | TB dispatch /<br>lookup | SIMD & other<br>helpers | BQL /<br>host kernel | unclass. | samples |
|---|---|---|---|---|---|---|---|---|
| `alu` (ILP=1, no memory) | **93.9%** | 0.4% | 0.0% | 0.8% | 0.1% | 3.9% | 1.0% | 9969 |
| `alu4` (ILP=4, no memory) | **93.3%** | 0.5% | 0.0% | 0.8% | 0.1% | 4.1% | 1.2% | 10000 |
| `ls4` (L1-resident memory) | **93.9%** | 0.5% | 0.1% | 0.7% | 0.1% | 3.9% | 0.8% | 9858 |
| `stream` (sequential 128 MiB) | 80.9% | 11.8% | 0.0% | 0.9% | 0.0% | 4.4% | 2.0% | 10047 |
| `nqueens` (branchy) | 69.3% | 3.0% | 0.0% | **22.4%** | 0.1% | 3.9% | 1.4% | 10078 |
| `matmul` (auto-vectorized) | 48.0% | 0.5% | 0.0% | 0.8% | **45.3%** | 4.8% | 0.6% | 10104 |
| **`7zz b` (real application)** | 48.6% | **31.7%** | 0.1% | 9.5% | 1.1% | 6.3% | 2.7% | 10144 |
| `syscall` (user↔kernel) | 29.2% | 5.0% | 0.1% | **30.4%** | 6.3% | **25.4%** | 3.7% | 10191 |
| `ptr256` (random 256 MiB) | 7.8% | **83.3%** | 0.0% | 0.8% | 0.2% | 3.1% | 4.8% | 10198 |
| `ls4tlb` (8192-page footprint) | **2.4%** | **88.5%** | 0.0% | 0.6% | 0.0% | 1.1% | 7.4% | 10185 |
| boot (translation-heavy) | 35.4% | 18.9% | **12.7%** | 11.7% | 0.9% | 10.1% | 10.2% | 1690 |
| ~~`ptr8`~~ | 89.1% | 1.4% | 0.8% | 1.6% | 0.0% | 6.3% | 0.8% | **494** ✗ |

`ptr8`'s row is struck through: only 494 usable samples, and they landed in its *warm* phase
(§5.3). It is not evidence of anything and is excluded from every conclusion below.

Six things fall out of this table:

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
   lots of indirect returns) spends **22.4%** in `cpu_tb_exec`/`tb_lookup`/`helper_lookup_tb_ptr`,
   versus 0.8% for the straight-line loops. `syscall` pays **30.4%**. TB chaining does not cover
   returns or exception entry — and `info jit` (§4.1) confirms only 59% of TBs get a direct jump at
   all. This is the second-largest measured lever after the MMU, and it is the one `12` does not
   rank anywhere.
4. **A real application lands in the middle, and its single biggest bucket is still the MMU.**
   `7zz b` — LZMA compression/decompression, a genuine workload with a multi-megabyte dictionary —
   spends **31.7% of host cycles in the softmmu slow path**, 9.5% in TB dispatch, and only 48.6%
   running translated code. This is the most representative single row in the table.
5. **~4–6% goes to the BQL even in a pure-compute guest, and 25% in a syscall-heavy one.** In
   `alu4` the vCPU thread is blocked in `__psynch_mutexwait`/`__psynch_mutexdrop` for 4.1% of
   samples, contending with the main loop thread for the big QEMU lock; in `syscall` it is 25.4%.
   This is QEMU-architecture overhead with no necessary analogue in a from-scratch design.
6. **`matmul`'s 42× is an SSE-emulation number, not an integer-codegen number.** 45.3% of its host
   time is in `helper_pshufd_xmm`, `helper_pmuludq_xmm`, `helper_punpckldq_xmm` and friends — QEMU
   calls out to C helpers for x86 vector ops. Do not let this workload into a "how bad is TCG on
   integer code" claim (see T-2).

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

## 5. Splitting the 8.6× into codegen and MMU — without double-counting

This is the section that replaces `12`'s "MMU+TSO 1.5–2.2× **×** codegen 1.3–1.8×".

### 5.1 The clean ablation: QEMU user-mode has no software MMU

Alpine ships `qemu-x86_64` (QEMU **11.0.1** linux-user), statically linked. Run **inside the
aarch64 HVF guest**, it executes the *same x86-64 `bench` binary* with the *same TCG backend* but
with **no software MMU at all** — linux-user uses the host MMU with a guest-base offset. The
denominator is the aarch64 `bench` binary running in that same VM, so the VM tax cancels exactly.

| Workload | qemu-user ÷ aarch64-in-VM<br>**(TCG codegen only)** | full-system TCG ÷ aarch64-in-VM<br>**(codegen + softmmu + kernel)** | what full-system adds |
|---|---|---|---|
| `alu` | 2.20× | 2.27× | 1.03× |
| `alu4` | 2.17× | 2.18× | 1.00× |
| `ls4` | 3.70× | 6.31× | **1.71×** |
| `ls4tlb` | **1.30×** | 31.4× | **24.1×** |
| `ptr8` | 1.07× | 12.8× | **12.0×** |
| `ptr256` | 2.61× | 12.6× | **4.84×** |
| `stream` | 12.1× | 13.7× | 1.13× |
| `matmul` (SIMD) | 33.1× | 42.1× | 1.27× |
| `nqueens` | 6.58× | 5.70× | 0.87× |
| `syscall` | 1.67× | 4.20× | **2.52×** |
| **geomean** | **3.51×** (2.73× excl. `matmul`) | **8.59×** (7.20× excl.) | **2.45×** (2.63× excl.) |

Read the first column as *"what QEMU's code generator costs you"* and the third as *"what QEMU's
software MMU and full-system kernel emulation cost you on top"*. On workloads with no memory traffic
the third column is exactly 1.00–1.03×, which is the sanity check that the decomposition is real.

Caveats on this table, none of which move the geomeans much:

* Alpine's `qemu-x86_64` is QEMU **11.0.1**; the full-system binary is **11.0.3**. Different builds,
  two patch releases apart.
* The third column for `syscall` (2.52×) is **not** MMU — linux-user translates guest syscalls
  directly to host syscalls, whereas full-system emulates the entire guest kernel path. That column
  is honestly labelled "softmmu **and** full-system kernel emulation".
* `nqueens` comes out at 0.87×, i.e. qemu-user is *slower* than full-system on it. Unexplained;
  most likely the 11.0.1/11.0.3 difference or user-mode's different TB-chaining behaviour across
  the `setjmp` boundary. It is the only row below 1.0 and it makes the geomean *more* conservative,
  not less.

### 5.2 Cross-check: instruction counts, marginal cost, and the host profile agree

The same split computed three completely independent ways (`probes/qemu_baseline/mmu_share.py`).
Guest memory-op counts come from a purpose-built TCG plugin (`plugin_count.c`); the boot baseline
(465.9 M memory ops, 1116 M instructions, measured with `natemu.task=none`) is subtracted.

```
fast-path marginal cost from the alu4/ls4 ablation: 0.776 ns per guest memory access

bench      t_tcg(s)  memops(G)  insns(G)  mem/insn   fast%   slow%  softmmu%  MMU-free
alu           0.900      0.003     5.106    0.075     0.3%    0.4%      0.7%     1.01x
alu4          0.926      0.003     8.705    0.048     0.3%    0.5%      0.8%     1.01x
ls4           2.782      2.407     9.913    0.260    67.1%    0.5%     67.7%     3.09x
ls4tlb       35.653      0.597     3.186    0.247     1.3%   88.6%     89.9%     9.85x
ptr256       17.597      0.829     1.266    0.544     3.7%   83.3%     86.9%     7.64x
stream        3.313      0.999     3.039    0.352    23.4%   11.8%     35.2%     1.54x
matmul        8.005      1.520     6.996    0.245    14.7%    0.5%     15.2%     1.18x
nqueens       1.716      1.065     3.264    0.350    48.2%    2.9%     51.1%     2.05x
syscall       1.442      0.400     0.970    0.415    21.5%    5.0%     26.5%     1.36x
```

**Is this workload set memory-representative?** The plugin also counted a real application. 7-Zip's
LZMA benchmark executes **34.1 G guest instructions with 8.58 G guest memory operations = 0.252
memory ops per instruction** — essentially identical to `ls4` (0.260) and `matmul` (0.245), and
below `stream` (0.352), `nqueens` (0.350), `syscall` (0.415) and Linux boot (0.417). So the
synthetic workloads are not memory-op-inflated relative to real code; if anything the *guest kernel*
is the most memory-dense thing measured here.

The plugin's memory-op count independently confirms the ablation's arithmetic: `ls4` minus `alu4`
is **2.404×10⁹** measured guest memory ops against **2.400×10⁹** predicted from the source
(0.17% error). The `MMU-free` column is `1/(1 − softmmu%)`, i.e. what QEMU would gain if guest
loads and stores became as cheap as they are on hardware — **geomean ≈ 2.0×**, and note the range:
**1.0× on register-only code, 3.1× on L1-resident memory-dense code, 7.6–9.9× once the address
footprint exceeds QEMU's software TLB.** This lands in the same place as the independent
qemu-user ablation's 2.45×, from a completely different measurement.

### 5.3 A warning about warm-up that anyone benchmarking QEMU TCG needs

`ptr8` costs **3.38 s the first time it runs in a fresh VM and 0.39–0.49 s on every subsequent
repetition in the same process** — an **8.7× swing with byte-identical work** (checksums match).
`alu4` repeated 25× shows no such effect (0.922–0.934 s, cv 0.5%), so this is not noise: it is
state inside QEMU. The magnitudes fit QEMU's *dynamically sized software TLB* (`accel/tcg/cputlb.c`
resizes only at flush points): cold, a 2048-page working set misses and pays ~100 ns/access; once
the TLB has grown to cover it, the same accesses cost ~13 ns.

**A controlled version of the effect**, repeating each workload inside one VM, with the HVF guest
run identically as the control (`sweep6.sh`; guest seconds per repetition):

| Workload | engine | rep 1 | 2 | 3 | 4 | 5 | 6 | first ÷ last |
|---|---|---|---|---|---|---|---|---|
| `ptr8` | **x86-64 TCG** | **3.438** | 0.484 | 0.390 | 0.414 | 0.384 | 0.384 | **8.95×** |
| `ptr8` | aarch64 HVF | 0.241 | 0.236 | 0.235 | 0.233 | 0.239 | 0.235 | 1.03× |
| `ls4tlb` | x86-64 TCG | 33.76 | 33.66 | 32.48 | 28.24 | | | 1.20× |
| `ls4tlb` | aarch64 HVF | 1.210 | 1.130 | 1.143 | 1.138 | | | 1.06× |
| `ptr256` | x86-64 TCG | 17.86 | 17.38 | 16.01 | | | | 1.12× |
| `ptr256` | aarch64 HVF | 1.360 | 1.323 | 1.245 | | | | 1.09× |
| `stream` | x86-64 TCG | 3.295 | 3.194 | 3.189 | | | | 1.03× |
| `ls4` | x86-64 TCG | 2.758 | 2.814 | 2.818 | | | | 0.98× |

**The control settles it:** the same guest binary, same guest kernel, same repetition pattern shows
a **1.03× drift under HVF and 8.95× under TCG**. It is QEMU state, not guest-OS or hardware
warm-up.

Consequences, all of which the corpus should absorb:

* **`ptr8`'s in-suite number (2.90 s, cv 13.6%) is contaminated** by `ls4tlb` having run before it.
  The tables above use the **solo, fresh-VM** number (3.351 s, cv 4.5%, n=5).
* The `ptr8` host profile was accidentally taken during the *warm* phase (494 usable vCPU samples,
  89% "translated code"), so **`ptr8` has no valid slow-path share** and is excluded from §5.2.
* **The headline workloads are safe.** `ls4tlb` drifts only 1.20× and `ptr256` 1.12× over repeats —
  and their HVF controls drift 1.06× and 1.09×, so most of even that is the guest allocator, not
  QEMU. The 88.6% / 83.3% slow-path shares are steady-state.
* **Any QEMU TCG number that does not state its warm-up protocol can be off by ~9× on a
  memory-resident workload.** That is an additional, independent reason to distrust the cited
  baselines `13` B-5 flagged — and a protocol requirement for anything we publish later.

---

## 6. MTTCG, and what the 8.6× is *per*

Everything above is `-accel tcg,thread=single`, `-smp 1` — i.e. **per guest vCPU**. QEMU's
multi-threaded TCG puts each vCPU on its own host thread. Guest task: N independent
single-threaded `alu4`+`ls4` workers, `-smp N`; wall clock from task start to `BENCHDONE`, n=3.

| `thread=` | `-smp` | workers | wall (s) | cv | vs 1 worker |
|---|---|---|---|---|---|
| single | 1 | 1 | 3.736 | 0.5% | 1.00× |
| single | 2 | 2 | 7.449 | 0.2% | 1.99× |
| single | 4 | 4 | 14.903 | 0.4% | 3.99× |
| multi | 1 | 1 | 3.728 | 1.1% | 1.00× |
| multi | 2 | 2 | 3.832 | 0.8% | **1.03×** |
| multi | 4 | 4 | 3.876 | 0.3% | **1.04×** |

**MTTCG scales at 96% efficiency here — 3.84× at 4 vCPUs** (14.903 s → 3.876 s). Single-threaded
TCG, as expected, serializes perfectly (1.99×, 3.99×).

Two caveats that matter more than the number:

* These four workers **share nothing**: separate processes, separate address spaces, no atomics
  across vCPUs, and therefore essentially no cross-vCPU TLB shootdowns and no contention on the TB
  cache beyond first fill. This is the **best case for MTTCG**. A genuinely multithreaded guest
  (shared data, `LOCK`-prefixed atomics, `INVLPG`/IPI shootdowns) is where MTTCG's costs live and
  none of that is exercised here. `02`'s exit-cost work and `10`'s IPI numbers are the relevant
  constraints there, not this table.
* The single-vCPU 8.6× therefore should be read as **"8.6× per core"**. On a fully parallel guest
  workload the M2 Max's 8 P-cores can hide it; on anything with a serial critical path they cannot.

### 6.1 QEMU knob sensitivity — is the baseline mis-tuned?

Medians of n=3, x86-64 TCG, seconds. This answers "did you just configure QEMU badly?".

| knob | `alu` | `alu4` | `ls4` | `ls4tlb` | `ptr8` | `ptr256` | `stream` | `matmul` | `nqueens` | `syscall` |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline `-cpu max` | 0.900 | 0.921 | 2.784 | 35.53 | 2.902 | 17.60 | 3.313 | 8.005 | 1.716 | 1.442 |
| `-cpu qemu64` | 0.899 | 0.925 | 2.783 | **31.39** | **2.529** | **15.35** | 3.122 | 8.099 | 1.691 | 1.435 |
| `-accel tcg,tb-size=1024` | 0.896 | 0.927 | 2.773 | 34.20 | 2.736 | 17.66 | 3.231 | 8.053 | 1.685 | 1.425 |

* **`-cpu max` costs nothing on compute and ~12% on memory-heavy workloads.** `alu`/`alu4`/`ls4`/
  `matmul`/`nqueens`/`syscall` move by <1%; `ls4tlb`, `ptr8`, `ptr256` are 12–13% faster under
  `-cpu qemu64`. The extra cost is in the software page-table walk, which under `-cpu max` has more
  paging features to honour (`14` notes that LA57 in particular must never be advertised to a guest
  we intend to shadow, since this host caps guest VA at 48 bits). **The headline is therefore ~10%
  conservative on memory workloads**, not optimistic.
* **`tb-size` is a no-op here.** `info jit` reports the code buffer as `…/1073724416` — the default
  is already 1 GiB — and **`TB flush count 0`** across every run. The code cache never overflowed,
  so none of these numbers are contaminated by translation-cache thrash. That also means the
  "translation/codegen" bucket being ~0% in steady state (§4) is real, not an artifact.

### 6.2 Transparent huge pages — the one place the comparison is arguably unfair to QEMU

Both guests boot stock Alpine with `THP: always`. The HVF guest's *hardware* TLB gets 2 MiB
entries from that; QEMU's *software* TLB is indexed at 4 KiB regardless of guest page size, so it
cannot. Re-running both sides with `transparent_hugepage=never` (n=3 each):

| | `ls4tlb` | `ptr256` | `stream` | geomean (10) | geomean excl. SIMD |
|---|---|---|---|---|---|
| TCG ÷ HVF, `THP=always` (default) | 31.3× | 12.6× | 13.7× | **8.46×** | 7.08× |
| TCG ÷ HVF, `THP=never` (both sides) | **17.8×** | **11.1×** | 12.8× | **7.73×** | 6.42× |

(Both rows here use the *in-suite* `ptr8` on both sides so the two configurations are compared
like-for-like; that is why the first row reads 8.46× rather than the 8.58× headline, which uses the
solo `ptr8` per §5.3. The difference is entirely one workload's warm-up state.)

Absolute effect: THP off makes the **HVF** guest 79% slower on `ls4tlb` (1.135 s → 2.033 s) and 30%
slower on `ptr256`, while barely touching TCG (+2%, +14%).

So **~9% of the headline 8.6× is "the hardware guest can use huge pages and QEMU's softmmu
structurally cannot"**. That is a genuine cost of software MMU emulation, not an artifact — but it
is a cost that a *shadow-paging* design also mostly inherits at 4 KiB granule (`14`), so it should
not be counted as headroom we would recover. **Quote 8.6× for the default configuration and 7.7× if
you want the huge-page advantage removed from the denominator.**

---

## 7. A real application: 7-Zip 26.01, the same upstream release everywhere

`12` rank 3's headline is a 7-zip number, so 7-zip is measured here directly — and, unlike the
figure it is quoting, **on one machine**. The *same upstream release* is used in all four
environments:

* `7z2601-mac.tar.xz` — a **universal Mach-O**, so native arm64 and Rosetta 2 run **literally the
  same file**, selected with `arch -arm64` / `arch -x86_64`.
* `7zip-26.01-r0.apk` from Alpine main, for the x86-64 and aarch64 guests.

Command: `7zz b -mmt1 -md=22` (single-threaded, 4 MiB dictionary — the classic configuration).
Medians of n=3. MIPS is 7-Zip's own rating; **higher is better**.

| Environment | compress MIPS | decompress MIPS | total | guest wall (s) |
|---|---|---|---|---|
| macOS arm64, native | 8314 | 7986 | 8150 | — |
| macOS x86-64, **Rosetta 2** | 7914 | 4762 | 6342 | — |
| Alpine aarch64, **HVF** | 9629 | 5445 | 7537 | 5.6 |
| Alpine x86-64, **TCG** | **893** | **1486** | **1185** | **27.6** |

**Only two of the six possible comparisons are valid**, and this is the trap the cited 4.4× figure
fell into:

| Valid comparison | compress | decompress | total |
|---|---|---|---|
| **x86-64 TCG ÷ aarch64 HVF** (same Alpine apk, same QEMU) | **10.8×** | **3.66×** | **6.36×** |
| **Rosetta 2 ÷ native arm64** (same universal binary) | 1.05× | 1.68× | 1.29× |
| ⇒ Rosetta 2 is faster than QEMU TCG by | **8.9×** | 3.2× | — |

**Invalid**: anything comparing the Linux-guest numbers to the macOS numbers. The Alpine build is
musl/GCC and the macOS build is Apple-clang; the aarch64 HVF guest *beats bare macOS* on compression
(9629 vs 8314 MIPS) and loses badly on decompression (5445 vs 7986), which is a compiler artifact,
not a virtualization effect. `05 §4.6`'s 16%-vs-71% comparison crosses exactly this boundary — and
also crosses two machines and two QEMU generations.

What the valid numbers say:

* **QEMU TCG's 7-Zip slowdown is 6.4× against the HVF control** — squarely inside the 8.6× geomean
  band from the synthetic suite, and closest to the `ls4`/`nqueens` region, as expected for LZMA.
* **Compression is 10.8× and decompression only 3.66×.** LZMA compression is the match finder: a
  large hash table, random access, branchy. Decompression is a tight range-decoder loop over a small
  window. That 3× spread inside a single application is the single best illustration of why one
  "QEMU is N× slower" number is not usable without knowing the memory behaviour.
* **Rosetta 2 lands at 78% of native overall (95% compressing, 60% decompressing)** — the cited
  "71% of native" figure is essentially confirmed on M2 Max with a 2026 7-Zip.

### 7.1 The softmmu share of a real application

Applying §5.2's method to 7-Zip, with all three inputs measured rather than modelled:

```
guest wall time under TCG                       27.63 s   (median of n=3, guest clock)
guest memory ops (plugin, boot baseline removed) 8.12 G
softmmu FAST path  = 8.12e9 x 0.776 ns =         6.30 s =  22.8% of runtime
softmmu SLOW path  (host profile, §4)                   =  31.7% of runtime
                                                   total =  54.5%
  -> removing the software MMU entirely would be         2.20x
```

**On a real application, over half of QEMU TCG's host time is the software MMU.** This is the
single most decision-relevant number in this document. For context, Tong et al. (TACO 2015)
measured 38.1% on SPEC under a 2014 QEMU; we measure 54.5% on LZMA under QEMU 11.0.3 on Apple
Silicon — same order, somewhat worse, on a workload with a 4 MiB working set.

### 7.2 The kernel-heavy workload: 400 × fork+exec

`7zz` and the microbenchmarks are all *user-mode* guest code. Process creation is the workload that
stresses the parts of the guest a compute benchmark never touches: page-table teardown, `CR3`
switches, TLB flushes (which force QEMU `tlb_flush`), and translating code at freshly mapped
addresses. Guest task: 400 sequential `fork`+`exec` of a static busybox, timed in-guest, n=3.

| Configuration | median | runs | cv |
|---|---|---|---|
| aarch64 guest, **HVF** | **0.040 s** | 0.04, 0.04, 0.04 | — |
| **x86-64 guest, TCG** | **1.240 s** | 1.24, 1.17, 1.33 | 5.3% |
| aarch64 guest, TCG | 1.270 s | 1.38, 1.27, 1.26 | 4.2% |

**≈31× slower than the HVF control — the worst full-workload ratio measured anywhere in this
document**, and 3.6× worse than the 8.6× compute geomean.

Two things to note, one of them a caveat:

* **It is not about x86.** aarch64-on-aarch64 TCG costs the *same* 1.27 s. Process creation cost
  under TCG is dominated by guest-kernel work and by QEMU's response to guest TLB invalidation, not
  by instruction translation quality. A Tier-2 design gets no automatic win here from a better
  code generator; it has to come from the MMU/TLB design.
* **Caveat: the HVF denominator is at the edge of its resolution.** `/proc/uptime` has 10 ms
  granularity, so 0.040 s carries ±5 ms, i.e. the ratio is 27–35× rather than exactly 31×. The
  conclusion (an order of magnitude worse than compute) is safe; the third digit is not.

For orientation: `12` quotes "kernel_build near 32×" from `07:345` as a cited figure. An
independently measured process-creation workload on this host lands at 27–35×. That particular
citation survives contact with reality.

---

## 8. What this means for `docs/research/12`

### (a) Rank 1 — "guest `ld/st` → host MMU is worth ~1.5× geomean" · **CONFIRMED, and it is an underestimate on this host**

`12` rank 1 claims **1.5× geomean**, sourced from HSPT's 1.51× and Captive's 2.21×, and supported by
a `1.74×` 9-instruction microbenchmark that `13` B-6 correctly called a lower bound.

Measured here, by **three independent methods that agree**:

| Method | What it measures | Result |
|---|---|---|
| `alu4`/`ls4` ablation + TCG-plugin memory-op counts + host profile (§5.2) | share of TCG runtime that is softmmu → `1/(1−share)` | **2.04× geomean** (1.01× register-only … 9.85× large-footprint) |
| qemu-user vs full-system, same binary, same VM (§5.1) | what full-system adds over a softmmu-free TCG | **2.45× geomean** (2.63× excluding SIMD) |
| **a real application** — `7zz b`, plugin counts + ablation + profile (§7.1) | softmmu share of host time on LZMA | **54.5%** → **2.20×** |

So **1.5× is safe as a floor and is probably 30–60% too low.** The most defensible single figure is
the real-application one: **2.20× on 7-Zip**, because it is a genuine workload and all three of its
inputs (27.63 s wall, 8.12 G memory ops, 31.7% slow path) were measured. Rank 1's
placement at the top of the table is correct and now rests on measurement rather than citation.

Three qualifications, all of which cut against over-claiming:

1. **This is an upper bound on the *win*, not a prediction of it.** It charges nothing for shadow
   page-table maintenance, nothing for `14`'s 256-ASID recycling problem, and nothing for
   fault-driven A/D (`HAFDBS = 0`). The realizable fraction is unknown and unmeasured.
2. **The workload mix over-weights memory** (T-3), though the 7-Zip instruction mix check in §5.2
   shows the *density* is representative (0.252 memory ops/instruction vs `ls4`'s 0.260).
3. **The literature agrees with the measurement to a suspicious degree, and that is reassuring.**
   Tong et al. (TACO 2015, `03 §6.1`) put memory emulation at **38.1%** of QEMU full-system time;
   we measure ~32% slow-path plus fast-path on 7-Zip and ~51% geomean over the whole suite. Bellard's
   "softmmu costs ~2× vs user-mode" (`03 §7`) is our 2.45×. BTMMU's 1.36–1.40× recovery ceiling
   (`03`) is *lower* than our 2.04× — consistent with BTMMU's SPECint mix being less memory-bound
   than ours.

**On `13` B-6 specifically:** the audit was right. The `1.74×` microbenchmark understates the cost.
The in-vivo marginal cost of a guest memory access is **0.776 ns under TCG vs 0.006 ns under HVF**,
and on a memory-dense loop that is a **3.09×** whole-loop effect, not 1.74×.

### (b) Rank 3 — "~4.4× QEMU→Rosetta codegen" · **REFUTED as stated; the real figure on this host is ~2.4×, and the 4.4× was never a codegen-only number**

`12` rank 3's 4.4× is `05 §4.6`'s **71% ÷ 16%**. Tracing it back:

* the **16%** is QEMU x86-64 **user-mode** 7-zip on a **Raspberry Pi 400 / Cortex-A72** with a
  2022-vintage QEMU (`05:534`);
* the **71%** is **Rosetta 2** 7-zip on an **Apple M1** running **macOS** (`05:544`).

**Those are two different machines, two different operating systems, two different 7-Zip builds and
two different years.** Dividing them produces a number that is not a property of any translator.
This is a stronger version of `13` B-5's objection and it was not previously logged.

Measured here, one machine, one 7-Zip release, one host:

| Quantity | `12`'s value | Measured on M2 Max |
|---|---|---|
| Rosetta 2 as % of native arm64 | 71% | **70%** (1.43× slowdown) — the cited figure holds up almost exactly |
| QEMU x86-64 **user-mode** as % of native-equivalent | 16% | **28%** (3.51× slowdown) — QEMU 11 is ~1.8× better than the 2022 number |
| **codegen-only ratio, QEMU-user ÷ Rosetta 2** | **4.4×** | **≈2.4×** |
| full-system TCG ÷ Rosetta 2 (codegen **and** MMU) | not stated | **6.3×** |

And the **code-expansion** half of rank 3 — "QEMU 17.4 host bytes per guest byte vs Captive ~10" —
**is confirmed**: `info jit` on QEMU 11.0.3 reports **15.7×** (316 host bytes / 20 guest bytes),
close to the cited 17.4.

**The structural point, which matters more than the arithmetic:** 4.4× was computed from a
comparison that spans *user-mode* QEMU on one side. Whatever "codegen headroom" it describes, it
does **not** contain the software-MMU difference, while the 6.3× full-system number does. So `12`'s
`Net` line — `MMU+TSO 1.5–2.2× **+** codegen 1.3–1.8× → 2.5–5×` — is not double-counting *because
the codegen factor came from user-mode*; it is instead **mixing a user-mode-derived codegen factor
with a full-system-derived MMU factor and hoping they compose.** `13` B-5 was right that the
projection is unsound, but the specific mechanism is different from the one it named.

The decomposition that *is* sound, because both halves were measured on the same host with the same
binaries, is in §5.1: **codegen 3.51× × (softmmu + kernel emulation) 2.45× = 8.59×**, and 3.51×
÷ Rosetta's 1.43× = **2.45× of codegen headroom** against a production translator.

### (c) "Net: Tier 2 = 2.5–5×" · **NOT REFUTED, but it now has a measured ceiling of ~6×, and 5× is not credible**

The corrected, measured statement:

```
  QEMU 11.0.3 full-system x86-64 TCG        8.59x   (vs the aarch64 HVF control, this host)
  QEMU 11.0.1 user-mode  x86-64 TCG         3.51x   (same TCG codegen, host MMU)
  Rosetta 2              x86-64             1.43x   (production codegen, host MMU, hardware TSO)
  aarch64 guest under HVF                   1.05x   (virtualization + guest OS + device model)
```

* **Ceiling.** A full-system emulator that matched **Rosetta 2's code generation** *and* eliminated
  the software MMU entirely would run at ≈1.43× of the HVF control, i.e. **≈6.0× faster than QEMU
  TCG** (8.59 ÷ 1.43). That is the hard ceiling measured on this host.
* **Why 5× is not credible.** Reaching 5× requires capturing 83% of that ceiling. But Rosetta 2 does
  **none** of the work a full-system emulator cannot avoid: no guest page tables, no privilege
  levels, no interrupt delivery, no device model, no guest kernel. The measured cost of the
  full-system parts that are *not* the MMU is already visible — `syscall` alone costs 2.52× more in
  full-system than in user mode (§5.1), and 25% of its host time is BQL/host-kernel.
* **What is defensible.** Splitting the ceiling: the MMU half is worth **2.0–2.45×** measured, and
  it is the half with the strongest evidence (`14` shows 4 KB shadow paging is *possible* on this
  host; this document shows it is *worth* the most). The codegen half is worth up to **2.45×** but
  requires FEXCore-class output. **A target of 2–4× over QEMU 11.0.3 on this host is defensible;
  2.5–5× should be restated as 2–4×, with 4× requiring both halves to land.**
* **And the baseline moved.** Captive's 2.21× was against QEMU circa 2016–2019. QEMU 11's user-mode
  x86-64 is already 1.8× better than the 2022 figure `05` cites. **Any speedup expressed as "× over
  QEMU" decays as QEMU improves** — which is the deepest reason `13` B-5's complaint mattered.

### Smaller corrections to `12` that fall out of this document

* **A kernel-heavy workload is missing from the table entirely.** Process creation is **≈31×**
  slower under TCG (§7.2) and — importantly — costs the *same* under aarch64-on-aarch64 TCG, so it
  is not a code-generation problem. `12` has no rank for "guest TLB invalidation / address-space
  switch cost", and on this evidence it belongs near the top.
* **Rank 3 should be split.** "Codegen quality" currently bundles instruction selection with TB
  dispatch. They are separable and behave differently: dispatch is **22.4%** of `nqueens` and
  **30.4%** of `syscall` but **0.8%** of straight-line loops, and `info jit` shows **41% of TBs have
  no direct jump**. Indirect-branch/return handling deserves its own rank.
* **A concern `12` does not list at all: the BQL.** 4.1% of a pure-compute guest's host time and
  25.4% of a syscall-heavy guest's is spent waiting on or dropping QEMU's big lock (§4). It is a
  QEMU-architecture artifact, so it inflates our baseline in our favour — it should be named as such
  rather than silently harvested as "our speedup".
* **`12`'s MTTCG-free framing.** All of `12`'s numbers are implicitly per-core. QEMU MTTCG already
  scales at **96% on embarrassingly parallel guest work** (§6), so a Tier-2 emulator gets no
  automatic parallel win over QEMU; the win must come from single-core execution.
* **Re-check rank 8.** `12` ranks A/D and TLB reach last. The measured cost of a *TLB-missing* guest
  access under TCG is **73 ns (≈255 cycles)**, and the workloads where that dominates are the ones
  with the largest TCG slowdowns. Whatever replaces softmmu inherits this sensitivity, and with
  `HAFDBS = 0` and 256 ASIDs (`14`) the replacement's miss path is the thing to design first.

---

## 9. Threats to validity — read this before quoting anything above

**Things that are solid (high confidence):**

* The 8.6× TCG-vs-HVF geomean, the 1.05× HVF-vs-native control, and the 0.776 ns/access softmmu
  fast-path cost. All are n=3 with cv < 4% on nearly every workload, reproduced across two
  independent sweeps hours apart, and the guest clock was independently cross-checked against the
  host wall clock.
* The `info jit` 15.7× expansion ratio and the 59% direct-jump rate: these are counters, not timings.
* The 7-Zip numbers: same upstream release everywhere, n=3, and the two valid comparisons are
  within-OS so no compiler difference enters them.
* The profile shares for `ls4tlb`, `ptr256`, `alu4`, `ls4`, `7zz`: ~10k samples each, and the buckets are
  overwhelming (88%, 83%, 93%, 94%), so classifier judgement calls cannot flip them.

**Things that are shakier, and why:**

| # | Issue | Effect |
|---|---|---|
| T-1 | **`ptr8` in-suite is order-dependent.** cv = 13.6% in the full suite (2.90 s median) but 3.35 s with cv = 4.5% when run alone in a fresh VM (n=5). Running `ls4tlb` first makes `ptr8` *faster*, consistent with QEMU resizing its software TLB upward at a flush after a high-miss window and the next workload inheriting the larger TLB. **Quote the solo number (12.8× vs HVF), not the in-suite one.** |
| T-2 | **`matmul`'s 42× is a SIMD-emulation number, not an integer-codegen number.** `-O2` auto-vectorizes it: NEON natively, SSE2 in the x86 guest, and TCG emulates x86 vector ops far worse than scalar ones (Rosetta also loses 2.89× here, its worst workload by far). Real SPECint-like code is mostly scalar. **This is why every geomean is also reported excluding `matmul`.** |
| T-3 | **The workload mix over-weights memory.** Five of ten workloads are deliberately memory-stress tests, because the MMU question is what the corpus needed answered. A SPECint-like mix would land lower than 8.6×; a database or kernel-build mix would land higher. Treat 8.6× as "this mix", not "all software". |
| T-4 | **`-cpu max` was used for the TCG guest.** That advertises the widest feature set, which may enable guest code paths (and, notably, possibly 5-level paging) that a narrower model would not. A `-cpu qemu64` sensitivity run is reported in §6.1: it changes compute workloads by <1% and memory workloads by 12–13%, in the direction that makes the headline **conservative**. |
| T-5 | **The host was not a quiet lab machine.** `vm.loadavg` was ≈ 2.8 at the start of the sweep — this is the user's working desktop. Measurements were serialized and repeated, and cv is low, but a background compile landing inside a run would not always be visible. The one time it demonstrably happened (a concurrent download) it produced cv = 50% on `ptr8`, and that sweep was discarded. |
| T-6 | **No `powermetrics`/`dtrace`.** `sudo` requires a password here, so P/E-core residency, frequency, and package temperature were never sampled directly. The E-core control (§2.4) shows all work landed on P cores, but that is an inference from timing, not a residency measurement. |
| T-7 | **Single host, single OS, single QEMU build.** M2 Max only, macOS 26.5.2 only, the Homebrew arm64 bottle of QEMU 11.0.3 only. Homebrew's build flags (not `--enable-debug`, glib from Homebrew, no LTO tuning) are not necessarily what a distro or a from-source build would produce. |
| T-8 | **`sample` is a 1 ms wall-clock sampler, not a PMU.** It cannot distinguish stalled cycles from retired instructions, and it attributes the *inlined* softmmu fast path to "translated code" (§4 item 1). The fast-path cost therefore had to be measured by ablation instead — which is the right instrument, but it means the profile alone would have understated the MMU cost by two orders of magnitude. |
| T-9 | **The fork+exec denominator is quantisation-limited.** 0.040 s under HVF is 4 ticks of `/proc/uptime`'s 10 ms clock, so §7.2's 31× is really 27–35×. |
| T-10 | **The "MMU-free" projection in §5 is an upper bound.** It charges *nothing* for the mechanism that would replace softmmu. `14-gate-results-verified.md` establishes 4 KB shadow paging is possible on this host, but shadow-page-table maintenance, 256-ASID recycling (`14`), and fault-driven A/D (`HAFDBS = 0`) all cost something that is not modelled here. |

**A summary of the measurement protocol, so the numbers can be audited:** every configuration ran
≥3 times (boot 5×, `ptr8` solo 5×); tables give medians; coefficients of variation are printed in
the tables or in `results/analysis_suite.txt`; the workloads that could not be made stable
(`ptr8` in-suite) are named and their conclusion is drawn from a different, stable run. Two full
sweeps hours apart agree to within 3% on every workload except `ptr8`.

**Things I could not measure, and why — stated rather than estimated:**

* **QEMU with `--enable-profiler`.** The TCG profiler was removed from QEMU before 11.0; there is no
  `-d prof` or `info profile` any more. `info jit` is what remains and it is reported in §4.1.
* **Hardware performance counters** (branch mispredicts, I-cache misses, dTLB misses) on the host.
  These need `powermetrics`/Instruments with elevated privileges. The audit's B-6 objection — that
  softmmu's real cost includes I-cache bloat and mispredicts — is therefore *not* directly confirmed
  here; the ablation measures the total marginal cost (0.776 ns) without attributing it to a
  microarchitectural mechanism.
* **A real distro boot to a login prompt** (systemd, disk, virtio). Deliberately avoided: it would
  have made the number I/O-bound and unreproducible. The boot number reported is kernel + initramfs
  + busybox shell, entirely in RAM.
* **SPEC CPU.** Not available and not licensable here. The workloads are hand-written ablations plus
  7-Zip; no claim is made that they proxy SPECint.

---

## 10. Reproducing this

```sh
cd probes/qemu_baseline
brew install qemu zig          # 11.0.3 / 0.16.0 were used here
./build.sh                     # downloads + SHA-256-verifies Alpine, builds bench for 4 targets
./fetch_7zip.sh                # 7-Zip 26.01 for all four environments + qemu-user for the guest
./sweep.sh                     # native / Rosetta / boot / the three guest configurations
./profile.sh                   # host sample profile + info jit + TCG plugin counts
./sweep1b.sh                   # the clean re-run that produced the published numbers
./sweep2.sh                    # MTTCG, QEMU knobs, THP, 7-Zip in all four environments
./sweep3.sh                    # isolated fresh-VM runs (the ptr8 order-dependence check)
./sweep4.sh                    # profile/plugin coverage gaps incl. 7-Zip
./sweep5.sh                    # QEMU linux-user x86-64 inside the aarch64 HVF guest
./sweep6.sh                    # warm-up characterisation (repeat the same workload in one VM)
./sweep7.sh                    # 400 x fork+exec, the kernel-heavy real workload
./analyze.py; ./analyze2.py; ./analyze_profile.py; ./mmu_share.py; ./final_numbers.py
```

`work/` (1.8 GB of Alpine images and kernels) is gitignored and re-fetched by `build.sh`;
`results/` is small and is committed. `results/dirty/` is the discarded first sweep (§2.4).

