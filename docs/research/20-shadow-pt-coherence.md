# 20 — How We Learn the Guest Edited Its Own Page Tables, and What It Costs

**Date:** 2026-08-08 · **Host:** Apple M2 Max, macOS 26.5.2, SDK Hypervisor 259.5.15
**Probes:** `probes/pt_coherence/pt_wp.c` (in-VM, guest EL1; raw log `probes/pt_coherence/run-all.log`)
· `probes/pt_coherence/ttbr1_split.c` (raw log `probes/pt_coherence/ttbr1_split.log`)
· `probes/pt_coherence/qemu/` (x86-64 Linux 6.18 guest under QEMU TCG + `plugin_inv.so`;
raw counts in `probes/pt_coherence/qemu/results/P.*.plugin.txt`)

`16-shadow-mmu-costs.md` measured what a shadow *fill* costs. It did not measure what keeping the
shadow *coherent with a guest that is editing its own page tables* costs, and `12`'s ranking has no
line item for it at all — `INVLPG` does not appear in `12` anywhere. This document closes that gap:
it states what x86 actually obliges the guest to do, checks what Linux actually does, and measures
the one design that has a per-PTE-write cost.

> **Reading the labels.** `M` = measured on this machine today, in-VM at guest EL1.
> `D` = derived by subtracting one measured quantity from another. `X` = modelled/extrapolated —
> arithmetic on an `M` number, not an observation. `C` = cited from a primary source (SDM, ARM ARM,
> Linux source). `?` = attempted and **inconclusive**, or **INFERRED** where explicitly marked.

---

## 0. Bottom line

### 0.1 The recommendation in one paragraph

**Do not write-protect the guest's page tables. Leave them writable and resync lazily, driven by
the guest's own `INVLPG` / `MOV CR3` / `MOV CR4` obligations.** The architecture makes this sound:
the SDM guarantees, in as many words, that a paging-structure entry going not-present → present
needs no invalidation *because such an entry is never cached* — so a lazy scheme can never miss a
new mapping, and the shadow-fill fault we already pay (`16 §2`, 146 ns) is exactly the mechanism
that discovers it. Every *other* transition the guest is architecturally obliged to announce. The
cost argument is decisive and is now measured on both ends: a write-protect trap costs
**33.3–34.3 ns** in its cheapest correct form and **231–274 ns** whenever the shadow entry it
replaces was valid, while a Linux `fork`+`exec` executes **18.1 `INVLPG` and 6.5 `MOV CR3` per
spawn** against an inferred **500–2000 PTE writes**. The lazy scheme's event rate is 30–110× lower
for the same information, and the per-event costs are within an order of magnitude of each other.
Write-protection is worth keeping for exactly one thing: **the top-level table (PML4) alone**, which
changes rarely and whose modification invalidates an entire shadow root.

### 0.2 The measured numbers this document rests on

| # | Quantity | Cost | Cyc @3.22 GHz | Tag |
|---|---|---|---|---|
| 1 | **`ESR_EL1.ISV` on a stage-1 permission fault at guest EL1** | **0, for all five store forms tested** | — | `M` |
| 2 | `ESR_EL1` on a write to a write-protected page (all forms) | `0x9600004f` — `EC=0x25`, `DFSC=0x0f`, `WnR=1` | — | `M` |
| 3 | A **read** of a write-protected PT page | **0 faults** | — | `M` |
| 4 | Bare permission fault + `ERET` (the floor) | **25.3–27.3 ns** | 82–88 | `D` |
| 5 | + read `ESR`/`FAR`, check `DFSC` and `WnR` | +0.0…+0.9 ns | — | `D` |
| 6 | + fetch the faulting insn from `ELR`, decode `Rt`, read the guest reg | +0.9…+1.8 ns | — | `D` |
| 7 | + apply the store to the guest PT page via a writable alias | **+0.0 ns** (below resolution) | — | `D` |
| 8 | + rmap lookup, PT frame → shadow L3 table | **+0.0 ns** (below resolution) | — | `D` |
| 9 | **+ synthesise and store the ARM descriptor — a complete, correct WP handler, INSTALL case** | **33.3–34.3 ns** | 107–111 | `D` |
| 10 | **+ `DSB`/`TLBI VAAE1IS`/`DSB`/`ISB` — the MODIFY case** | **231–274 ns** | 745–884 | `D` |
| 11 | The same ladder, one PTE in each of 512 PT pages (scattered) | 39.1–45.5 ns / 274–351 ns absolute | — | `M` |
| 12 | Trap-every-store, per store, flat over N = 1…512 | **33.6–34.7 ns** | ~110 | `M` |
| 13 | Unprotect → N native stores → re-protect → resync 512 entries, **no** closing TLBI | **800–930 ns per episode**, flat in N | — | `M` |
| 14 | …the same with a closing `TLBI VMALLE1IS` | **1030–1200 ns per episode** | — | `M` |
| 15 | **Break-even burst length**, trap-each vs unprotect-and-resync | **N ≈ 32 (no TLBI) / N ≈ 64 (TLBI)** | — | `D` |
| 16 | `TCR_EL1` accepts **`T0SZ = T1SZ = 17`** with `EPD1=0` — split exactly at bit 47 | works, 17/17 checks pass | `M` |
| 17 | **TTBR1 global (`nG=0`) working set survives a fresh-ASID TTBR0 write** | **+0.054 ns/page** (0.2 µs / 12 MiB) | — | `D` |
| 18 | TTBR1 **non-global** working set, same event | **+19.7 ns/page** (60.5 µs / 12 MiB) | — | `D` |
| 19 | TTBR0 working set, same event | +20.7 ns/page | — | `D` |
| 20 | `TLBI ASIDE1IS` against a global TTBR1 set (must not evict) | **+0.46 ns/page — does not evict** | — | `D` |
| 21 | `TLBI VMALLE1IS` against the same set (must evict) | +21.1 ns/page — evicts | — | `D` |
| 22 | **`INVLPG` per `fork`+`exec`**, Linux 6.18 x86-64, busybox `true` × 2000 | **18.1** | — | `M` |
| 23 | **`MOV CR3` per `fork`+`exec`** | **6.5** (smp1) / **5.1** (smp4) | — | `M` |
| 24 | Guest page faults per `fork`+`exec` (guest `/proc/vmstat`) | **59.3** | — | `M` |
| 25 | Guest instructions per `fork`+`exec` | **939 k** | — | `M` |
| 26 | `INVLPG` per 10⁹ guest instructions, **7-Zip benchmark** | **7.1** (smp1) / **3.8** (mt4) | — | `M` |
| 27 | `INVLPG` per 10⁹ guest instructions, ALU/memory microbench suite | **1.6** | — | `M` |
| 28 | `INVLPG` per 10⁹ guest instructions, `fork`+`exec` storm | **19 302** | — | `M` |

**Reference points this must be compared against** (`02`, `12`, `14`, `16`, `19`): a bare `HVC`
round trip to the host is **808 ns**; a full in-VM shadow fill is **146 ns** without a TLBI and
**439 ns** with one; any single-page `TLBI` plus its barriers is **279–285 ns**; an `ASID` switch
is **10.3 ns**; a permission fault is **3.3× cheaper** than a translation fault.

### 0.3 The cost model that decides the question

Per unit of guest work, the two candidate designs pay:

| | write-protect | lazy resync |
|---|---|---|
| per guest **PTE write** | **33.4 ns** (install) … **≥ 270 ns** (modify) `M` | 0 |
| per guest **`INVLPG`** | 0 (shadow is already exact) | ~280 ns (drop shadow PTE + `TLBI`) `M` (`16 §3`) |
| per guest **`MOV CR3`** | ASID switch, 10.3 ns `M` (`16 §5`) | ASID switch, 10.3 ns |
| per **touched** guest page | 0 if eagerly filled | 146 ns shadow fill `M` (`16 §2`) |
| events per `fork`+`exec` | **500–2000** `INFERRED` | **18.1** `INVLPG` + 6.5 `CR3` `M` |

Multiplying out for one `fork`+`exec` of a static binary (§5): write-protection costs
**17–67 µs** and lazy resync costs **5.1 µs** of invalidation work. That is the whole argument, and
it survives being wrong about the inferred PTE-write count by a factor of three in either direction.

### 0.4 The five findings that change the design

1. **`ESR_EL1.ISV` is 0 for every stage-1 permission fault at guest EL1.** Measured on five
   distinct store encodings (`STR X`, `STRB W`, `STR W`, `STP X,X`, `STUR`): the syndrome is
   *bit-for-bit identical*, `0x9600004f`, in all five cases. It carries the fault class and `WnR`
   and nothing else — no transfer register, no access size, no operand count. **Any design that
   traps guest stores must fetch and decode the faulting instruction itself.** That is cheap here
   (+0.9…+1.8 ns) only because our faulting instruction is a simple `STR`; `STP` writes *two*
   guest PTEs from one fault, and an atomic `LDXR`/`STXR` or `CAS` on a write-protected line cannot
   be emulated by "decode, apply, `ERET`" at all (§6, UNRESOLVED).
2. **Everything in the write-protect handler except the `TLBI` is free.** Reading `ESR`/`FAR`,
   decoding the instruction, applying the store, doing an rmap hash lookup and synthesising an ARM
   descriptor together add **7 ns** to a 27 ns fault. Adding one `TLBI` adds **200–240 ns**. The
   handler's cost is not its logic; it is entirely (a) the exception round trip and (b) whether it
   must invalidate. This is the same shape `16 §2` found for the fill path, confirmed independently
   on a different path.
3. **Trapping every store never amortises, and unprotect-and-resync only wins above N ≈ 32.**
   Trap-each is a flat **33.6–34.7 ns per store** from N = 1 to N = 512 — there is no batching
   effect to exploit, because each store pays its own exception. The alternative (unprotect the PT
   page, let the guest run, re-protect, re-derive all 512 shadow entries) costs a flat
   **~830 ns per episode** regardless of N, dominated by two protection flips and their `TLBI`s.
   They cross at **N ≈ 32**. A guest that writes one PTE and moves on — the common case outside
   `fork` — is on the wrong side of that line either way.
4. **The kernel half in `TTBR1`, marked global, is free across guest `CR3` switches, and this is
   now executed rather than asserted.** `T0SZ = T1SZ = 17` is accepted by `TCR_EL1` under HVF, puts
   the ARM VA split *exactly* on x86's canonical boundary at bit 47, and a 12 MiB kernel-half
   working set survives a fresh-ASID `TTBR0` write at **+0.054 ns/page** — against **+20.7 ns/page**
   for the same working set in `TTBR0`. `TLBI ASIDE1IS` does not evict it (+0.46 ns/page);
   `TLBI VMALLE1IS` does (+21.1 ns/page). This makes the guest's high half a *coherence-free* region
   across the most frequent guest MMU event there is.
5. **The event rate that decides the whole question is measured, and it is small.** A real Linux
   6.18 x86-64 guest issues **1.6 to 7.1 `INVLPG` per 10⁹ instructions** on compute workloads and
   **19 302 per 10⁹** in a `fork`+`exec` storm. At 280 ns per handled `INVLPG`, even the storm costs
   **0.54 % of a core**; the compute workloads cost **2 parts per million**.

### 0.5 The design rule

> **The guest's page tables stay writable. The shadow is repaired lazily, on the guest's own
> `INVLPG` / `MOV CR3` / `MOV CR4` / `MOV CR0` events and on shadow-miss faults, and never by
> trapping a store to a page-table page — with one exception: the top-level table (PML4) may be
> write-protected, because it is written rarely and each write invalidates a whole shadow root.**
>
> The rule is legal only because of one sentence in the SDM (§2), and it is affordable only because
> the guest's invalidation rate is three to five orders of magnitude below its store rate (§5).

### The single most important number

> **A guest `fork`+`exec` performs 18.1 `INVLPG` and 6.5 `MOV CR3` — and somewhere between 500 and
> 2000 page-table writes.**
>
> The first two are measured. The third is inferred. But the *ratio* is what matters, and no
> plausible value of the third puts write-protection ahead: at the measured 33.4 ns per trap and
> 280 ns per handled `INVLPG`, write-protection has to beat lazy resync by seeing fewer than
> **8.4× more** events than lazy resync does. It sees **30–110×** more.
>
> **It is not that write-protection is expensive per event. It is 8× *cheaper* per event. It loses
> because it fires 30–110× as often.**

---

## Method and its guards

* **Clock.** `CNTFRQ_EL0 = 24 MHz` (41.67 ns/tick), so every measurement batches ≥ 512 iterations
  into a timed window and divides. The burst experiment additionally repeats each episode
  `M = 16384/N` times inside the window, so even the N = 1 arm spans hundreds of ticks. Clock
  estimated from a 16-deep dependent `ADD` chain: **3.22 GHz**; cycle columns are `X`, ±5 %.
* **Every fault measurement has a matched control.** The faulting loop and the control loop execute
  *identical* instruction sequences over *identical* address streams against *identical* geometry;
  only the target region's `AP[2]` bit differs. The reported NET is the difference.
* **Two-pass guard, and it caught a real artifact.** `16 §1` reported that the first configuration
  executed in a process reads 3–4× high. It happens here too and it is worse than "the first":
  **the first three configurations in the sequence read 2.7–4× high**, which made steps 0–2 of the
  ladder look *more* expensive than steps 3–5. Every configuration is therefore run **twice** and
  both passes are printed; only pass 2 is quoted. A repeat of step 0 at the *end* of the ladder
  (`0'`) confirms no drift in the other direction — it lands within 1 ns of pass-2 step 0 in every
  run. *A ladder measured in one pass would have produced a confidently wrong, non-monotonic
  breakdown here.*
* **Correctness assertions, not just timings.** The probe asserts that exactly
  `iterations × rounds` faults were taken (51 712 in every ladder arm, no more and no fewer), and
  for every handler that applies the store it verifies afterwards, from the host, that all 512
  guest PTEs actually hold the written value. Both are checked on every arm.
* **Hang safety.** Every handler carries a fault counter and a limit and self-traps via `HVC #9`
  past it; every guest run is covered by a host watchdog thread calling `hv_vcpus_exit()`. Neither
  fired in the final runs.
* **rmap hash verified, not assumed.** The handler's `(frame ^ (frame >> 5)) & 0x3ff` hash is
  checked host-side to be collision-free across all 512 PT frames, so the measured lookup is the
  hit path we intended and not a probe sequence we did not model. (An earlier version had the host
  choosing the shift dynamically while the payload hard-coded it — the mismatch drove every lookup
  into the panic path, which is how it was found.)
* **Spread.** Each figure is the median of 101 in-guest rounds (ladder) or 25 (burst). Headline
  ladder figures were repeated across **four independent process invocations**; the ranges in §0.2
  are the spread across those invocations, not within one.
* **Guest-workload counts.** The x86 side is a Linux 6.18.35 x86-64 guest under QEMU TCG with a
  TCG plugin counting instruction classes inline (one host `add` per event), cross-checked against
  the guest's own `/proc/vmstat` and `/proc/stat` deltas taken inside the same boot. Per-workload
  figures are **deltas against a matched `task=none` boot of the same image**, so boot-time
  activity is subtracted rather than amortised.

---

## 1 — What the x86 architecture actually obliges the guest to do

All quotes verbatim from **Intel SDM Vol. 3A, order number 325384-092US, June 2026**
(`https://cdrdv2.intel.com/v1/dl/getContent/671447`, SHA-256
`5b77208f34f7220b489db1642a681153e19dea5251a32973c01cb9e760bd0cce`), text extracted with
`pdftotext -layout` and each quote re-verified by hand against the extracted text.

> ⚠️ **Citation hygiene, corpus-wide.** Paging is **Chapter 5** in the current SDM, not Chapter 4.
> `06 §2.1` cites "SDM Vol.3A §4.10" and `06 §2.5` cites "§4.8"; Linux's own `fault.c` cites
> "Vol 3 Section 4.10.4.3". Those numbers were right for older editions and the subsection suffixes
> are unchanged — `4.10.4.3` → **`5.10.4.3`** — but doc 20 and anything downstream should cite the
> `5.10.x` numbering.

### 1.1 The load-bearing rule: not-present → present needs no invalidation

**SDM §5.10.4.3, "Optional Invalidation", first bullet** (`C`):

> "If a paging-structure entry is modified to change the P flag from 0 to 1, no invalidation is
> necessary. This is because no TLB entry or paging-structure cache entry is created with
> information from a paging-structure entry in which the P flag is 0.¹"

This is the sentence the entire lazy-resync design rests on, and the answer to the crux question is
therefore **yes, unambiguously: a guest may legally install a new mapping with no invalidation at
all and expect it to work.** Which means an invalidation-driven scheme *cannot* be told about new
mappings — and does not need to be, because the shadow simply faults on first use.

**But footnote 1 is load-bearing and is easy to miss** (`C`):

> "1. If it is also the case that no invalidation was performed the last time the P flag was changed
> from 1 to 0, the processor may use a TLB entry or paging-structure cache entry that was created
> when the P flag had earlier been 1."

The P 0→1 exemption is therefore *conditional on the preceding* P 1→0 *having been invalidated*.
For us this is good news, not bad: it means a guest that unmaps without invalidating has already
forfeited any guarantee, so a stale shadow entry after such a sequence is architecturally
permitted behaviour, not a bug in our emulator.

The complementary statement, **SDM §5.10.2.3 "Details of TLB Use"** (`C`):

> "Because the TLBs cache entries only for linear addresses with translations, there can be a TLB
> entry for a page number only if the P flag is 1 and the reserved bits are 0 in each of the
> paging-structure entries used to translate that page number. In addition, the processor does not
> cache a translation for a page number unless the accessed flag is 1 in each of the
> paging-structure entries used during translation; before caching a translation, the processor sets
> any of these accessed flags that is not already 1."

> ⚠️ **A correction to a widely-repeated paraphrase.** The sentences "no TLB entry is created for a
> linear address whose translation would cause a page fault" and "the processor does not cache a
> translation for a page marked not present" **do not appear in the SDM.** A full-text search of the
> 1602-page volume returns zero hits for the first and one hit for the second — and that one hit is
> about the *accessed* flag, not the P flag. The manual states it as a **necessary condition**
> ("there can be a TLB entry ... only if"), never as an affirmative promise, except inside the
> §5.10.4.3 justification clause quoted above. `06 §2.1`'s wording — "the processor is forbidden
> from caching a translation for a non-present page" — is a fair reading of the two together but is
> not a quote, and should not be presented as one.

**Second exemption, and it is the one that lets us be lazy about the A bit** (`C`, §5.10.4.3):

> "If a paging-structure entry is modified to change the accessed flag from 0 to 1, no invalidation
> is necessary (assuming that an invalidation was performed the last time the accessed flag was
> changed from 1 to 0)."

### 1.2 The permission-upgrade exemptions: spurious faults, not incoherence

Three further §5.10.4.3 bullets, verbatim (`C`):

> "If a paging-structure entry is modified to change the R/W flag from 0 to 1, failure to perform an
> invalidation may result in a "spurious" page-fault exception (e.g., in response to an attempted
> write access) but no other adverse behavior. Such an exception will occur at most once for each
> affected linear address (see Section 5.10.4.1)."
>
> "If CR4.SMEP = 0 and a paging-structure entry is modified to change the U/S flag from 0 to 1,
> failure to perform an invalidation may result in a "spurious" page-fault exception ... but no
> other adverse behavior."
>
> "If a paging-structure entry is modified to change the XD flag from 1 to 0, failure to perform an
> invalidation may result in a "spurious" page-fault exception ... but no other adverse behavior."

> ⚠️ **This refutes `06 §2.4` on a point of correctness.** `06 §2.4` says of lazy resync: *"The one
> thing lazy resync cannot handle is a present→present permission upgrade without invalidation —
> which the architecture forbids the guest from doing, so it is safe."* **The architecture does not
> forbid it.** It explicitly permits it and specifies the consequence: a spurious `#PF`, at most
> once per linear address. Linux relies on this by design (§3.2).
>
> The conclusion `06` drew is nevertheless still correct, for a different and better reason: a
> shadow design that keeps entries *present but restricted* takes a **permission fault** on the
> upgraded access, walks the guest tables, sees the upgrade, repairs the shadow and retries. We
> therefore *inherit the guest's spurious-fault budget as our repair trigger*. The mechanism that
> makes lazy resync safe against permission upgrades is not a prohibition on the guest — it is our
> own fault handler, at 27 ns a time.

The reverse direction — permission **downgrade** without invalidation — is genuinely not exempted;
§5.10.4.2 requires an `INVLPG` and §5.10.4.4 "Delayed Invalidation" spells out that until it
happens "the processor may use translations based on either the old value or the new value". A
guest that downgrades without invalidating gets undefined behaviour on real hardware and gets the
same from us. **This is the property that makes lazy resync sound and it is the only one we need.**

### 1.3 What software *is* required to do

**SDM §5.10.4.2 "Recommended Invalidation"**, first two items (`C`):

> "If software modifies a paging-structure entry that maps a page (rather than referencing another
> paging structure), it should execute INVLPG for any linear address with a page number whose
> translation uses that paging-structure entry."
>
> "If software modifies a paging-structure entry that references another paging structure, it may
> use one of the following approaches ... Execute INVLPG for linear addresses with each of the page
> numbers with translations that would use the entry. However, if no page numbers that would use the
> entry have translations (e.g., because the P flags are 0 in all entries in the paging structure
> referenced by the modified entry), **it remains necessary to execute INVLPG at least once.**"

That last clause is a gift: even a completely empty sub-tree being linked in costs the guest one
`INVLPG`, so an upper-level link is always announced to us.

**§5.10.4.1** lists the invalidating operations (`C`): `INVLPG`, `INVPCID`, `MOV to CR0` (when
`PG` 1→0), `MOV to CR3`, `MOV to CR4`, task switch, VMX transitions.

> ⚠️ **Correction to a plausible-looking list.** For `MOV to CR4`, only these are *required* to
> invalidate: a change to `CR4.PGE`; `CR4.PCIDE` 1→0; a change to `CR4.PAE`; `CR4.SMEP` 0→1.
> **`CR4.PSE` and `CR4.SMEP` 1→0 are not required** — they appear only in the permissive list
> ("The processor is always free to invalidate additional entries"). **`CR4.SMAP` is not mentioned
> at all.** `06 §2.6`'s list of "whole-shadow invalidators" — `CR4.PGE`, `SMEP`/`SMAP`, `PAE`,
> `PCIDE`, `LA57`, `EFER.NXE` — is therefore a list of things we may *choose* to treat as
> whole-shadow flushes, not a list the guest is obliged to announce. For `SMAP` and `SMEP` 1→0 in
> particular we must flush on our own initiative because the guest will not tell us. Since all are
> rare, flushing on any `CR4`/`EFER`/`CR0` write that changes a paging-relevant bit is the right
> implementation, and `06`'s conclusion stands even though its premise was wrong.

Also worth having, because it means the guest's own fault path helps us (`C`, §5.10.4.1):

> "In addition to the instructions identified above, page faults invalidate entries in the TLBs and
> paging-structure caches. In particular, a page-fault exception resulting from an attempt to use a
> linear address will invalidate any TLB entries that are for a page number corresponding to that
> linear address and that are associated with the current PCID."

### 1.4 Paging-structure caches

**§5.10.3.1** enumerates a PML5E cache, PML4E cache, PDPTE cache and PDE cache, and for each says
(`C`, PDE case shown):

> "If the processor creates a PDE-cache entry, the processor may retain it unmodified even if
> software subsequently modifies the corresponding PML5E, PML4E, PDPTE, or PDE in memory."
>
> "The processor may create a PDE-cache entry even if there are no translations for any linear
> address that might use that entry."

and closes the section with the obligation (`C`):

> "Because the processor may create the cache entries at the time of translation and not update them
> following subsequent modifications to the paging structures in memory, software should take care
> to invalidate the cache entries appropriately when causing such modifications."

**Consequence for us, and it is a favourable one.** The guest is obliged to invalidate after
changing *any* level, not just the leaf. So an upper-level edit is announced by the same `INVLPG`
we already watch. Note also the second bullet: x86 permits caching an upper-level entry *with no
live translation beneath it*, which is strictly more aggressive than what ARM's stage-1 walker is
permitted to do with an invalid descriptor — so our shadow is never obliged to model a construct
the hardware cannot represent.

### 1.5 Global pages and PCID

**§5.10.2.4 "Global Pages"**, the whole section (`C`):

> "The Intel-64 and IA-32 architectures also allow for global pages when the PGE flag (bit 7) is 1
> in CR4. If the G flag (bit 8) is 1 in a paging-structure entry that maps a page ... any TLB entry
> cached for a linear address using that paging-structure entry is considered to be global. Because
> the G flag is used only in paging-structure entries that map a page, and because information from
> such entries is not cached in the paging-structure caches, the global-page feature does not affect
> the behavior of the paging-structure caches.
>
> A logical processor may use a global TLB entry to translate a linear address, even if the TLB
> entry is associated with a PCID different from the current PCID."

The invalidation behaviour lives in §5.10.4.1, not here (`C`): `INVLPG` "also invalidates any global
TLB entries with that page number, regardless of PCID", while `MOV to CR3` with `CR4.PCIDE = 0`
"invalidates all TLB entries associated with PCID 000H **except those for global pages**".

**PCID**, §5.10.4.1, all three `MOV to CR3` cases (`C`):

> "If CR4.PCIDE = 0, the instruction invalidates all TLB entries associated with PCID 000H except
> those for global pages. ...
> If CR4.PCIDE = 1 and bit 63 of the instruction's source operand is 0, the instruction invalidates
> all TLB entries associated with the PCID specified in bits 11:0 of the instruction's source operand
> except those for global pages. ... It is not required to invalidate entries in the TLBs and
> paging-structure caches that are associated with other PCIDs.
> If CR4.PCIDE = 1 and bit 63 of the instruction's source operand is 1, the instruction is not
> required to invalidate any TLB entries or entries in paging-structure caches."

`INVPCID` types, verbatim (`C`): type 0 "Individual-address ... invalidates mappings—except global
translations—associated with the PCID specified ... and that would be used to translate the linear
address specified"; type 1 "Single-context ... all mappings—except global translations—associated
with the PCID specified"; type 2 "All-context, including globals ... including global translations
—associated with all PCIDs"; type 3 "All-context ... except global translations—associated with all
PCIDs".

> **A structural point the corpus has not made.** Almost every one of these is phrased "is not
> required to invalidate" or "may invalidate", not "does not invalidate". x86's invalidation
> semantics are a *lower bound* on what the processor does. **Over-invalidating is always
> architecturally legal**, which means our shadow may always be more aggressive than the guest
> asked for. That is what makes it safe to map x86's core-local `INVLPG` onto ARM's
> hardware-broadcast `TLBI VAE1IS` (§6.2) — the mapping over-approximates, and
> over-approximation is free of correctness risk.

---

## 2 — What Linux actually does, versus what the SDM permits

Verbatim from Linux **v6.12**, retrieved from the `torvalds/linux` mirror at the `v6.12` tag.
(The guest measured in §5 is 6.18.35; the code below is unchanged between those releases in every
respect that matters here, but the quotes are v6.12 and should be read as such.)

### 2.1 Linux relies on the not-present → present rule, explicitly and in four places

`arch/x86/include/asm/tlbflush.h:349-370` (`C`):

```c
/*
 * pte_needs_flush() checks whether permissions were demoted and require a
 * flush. It should only be used for userspace PTEs.
 */
static inline bool pte_needs_flush(pte_t oldpte, pte_t newpte)
{
	/* !PRESENT -> * ; no need for flush */
	if (!(pte_flags(oldpte) & _PAGE_PRESENT))
		return false;
	...
}
```

The reasoning is in the header comment of `pte_flags_need_flush()`, same file, lines 296–347 (`C`):

> `Non-present entries are not cached. Hardware would set the dirty/access bit if needed without a fault.`

And the demand-fault install sites carry it as a comment next to a `set_ptes()` with **no**
`flush_tlb_*` call at all — `mm/memory.c:4837` in `do_anonymous_page()`, `mm/memory.c:4605` in
`do_swap_page()`, and `mm/memory.c:759` in `restore_exclusive_pte()` (`C`):

```c
	/* No need to invalidate - it was non-present before */
	update_mmu_cache_range(vmf, vma, addr, vmf->pte, nr_pages);
```

with a fourth, differently-worded instance in `set_pte_range()`, `mm/memory.c:5044` (`C`):

```c
	/* no need to invalidate: a not-present page won't be cached */
```

> ⚠️ **A correction worth recording, because it is the kind of thing that gets fabricated.** There
> is **no** comment reading "If the PTE was not present, there is no need to flush" anywhere in the
> tree. The actual text is the terse `/* !PRESENT -> * ; no need for flush */`, byte-identical to
> what Nadav Amit merged in commit `c9fe66560bf2` ("mm/mprotect: do not flush when not required
> architecturally", 2022, for v6.0) and unchanged since. Quote the real one.

**So: does Linux always invalidate? No — and the exceptions are exactly the SDM's exemptions.**
Linux's `pte_flags_need_flush()` encodes the required set precisely (`C`):

* `flush_on_clear = _PAGE_DIRTY | _PAGE_PRESENT | _PAGE_ACCESSED` — flush if any of these were
  **cleared**;
* `flush_on_change = _PAGE_RW | _PAGE_USER | _PAGE_PWT | _PAGE_PCD | _PAGE_PSE | _PAGE_GLOBAL |
  _PAGE_PAT | _PAGE_PAT_LARGE | _PAGE_PKEY_BIT0..3 | _PAGE_NX` — flush on **any** change;
* a PFN change — always flush;
* `_PAGE_SOFTW1..4`, `_PAGE_SAVED_DIRTY` — never flush;
* `_PAGE_ACCESSED` ignored for PTEs (`ignore_access=true`) but **not** for huge PMDs.

Note that `flush_on_change` is *stricter* than the SDM requires: Linux flushes on `_PAGE_RW` 0→1
and `_PAGE_NX` 1→0 in the `mprotect` path even though §5.10.4.3 exempts both. Linux's own commit
message says so (`C`): *"Always flush the TLB when it is architecturally needed even when skipping a
TLB flush might only result in a spurious page-faults by skipping the flush."*

Also note `include/asm-generic/tlb.h:737-749`: any architecture that has not opted in gets
`pte_needs_flush() { return true; }`. **The optimisation is x86-specific and opt-in.**

### 2.2 Where Linux *does* take the spurious-fault path

Two places, and neither goes through `pte_needs_flush()`.

**Kernel mappings**, `arch/x86/mm/fault.c:951` (`C`):

```c
/*
 * Handle a spurious fault caused by a stale TLB entry.
 *
 * This allows us to lazily refresh the TLB when increasing the
 * permissions of a kernel page (RO -> RW or NX -> X).  Doing it
 * eagerly is very expensive since that implies doing a full
 * cross-processor TLB flush, even if no stale TLB entries exist
 * on other processors.
 *
 * Spurious faults may only occur if the TLB contains an entry with
 * fewer permission than the page table entry.  Non-present (P = 0)
 * and reserved bit (R = 1) faults are never spurious.
 * ...
 * See Intel Developer's Manual Vol 3 Section 4.10.4.3, bullet 3
 * (Optional Invalidation).
 */
```

**User COW re-use**, `arch/x86/mm/pgtable.c:515` (`pmdp_set_access_flags`; `pudp_set_access_flags`
carries the identical comment) (`C`):

```c
	if (changed && dirty) {
		set_pmd(pmdp, entry);
		/*
		 * We had a write-protection fault here and changed the pmd
		 * to to more permissive. No need to flush the TLB for that,
		 * #PF is architecturally guaranteed to do that and in the
		 * worst-case we'll generate a spurious fault.
		 */
	}
```

and `ptep_set_access_flags()` at `arch/x86/mm/pgtable.c:495` performs **no TLB flush at all** — it
conditionally writes the PTE and returns. `wp_page_reuse()` (`mm/memory.c:3229`) calls it and then
`update_mmu_cache_range()`, never `flush_tlb_*`.

**This is the single most important behavioural fact in this section for us.** On the RO→RW COW
path — one of the hottest page-table operations a Linux guest performs — **Linux upgrades a live,
present PTE and deliberately does not invalidate.** A lazy scheme keyed purely on `INVLPG` would
never hear about it. It does not have to: our shadow keeps the page read-only, the guest's next
write takes a **permission fault at 27 ns** (`M`), we walk, see `RW=1`, repair, retry. `06 §2.4`'s
claim that the architecture forbids this is wrong; the design survives anyway because the repair
trigger is our own fault, not the guest's announcement.

### 2.3 vmalloc lazy flush, and huge-page splits

**vmalloc.** Linux batches the flush for freed vmalloc ranges (`lazy_max_pages()`,
`purge_vmap_area_lazy()`): a freed area's mappings may remain in the page tables and in the TLB
until a purge. This is a *teardown* deferral, not a mapping-installation one, and it deferrals only
the invalidation, not the `set_pte` — the PTE clear does happen. For us the effect is that the
window between "guest cleared a kernel PTE" and "guest issued the flush" can be long. **That is
architecturally exactly the P 1→0 without invalidation case**, in which the SDM permits the
processor to keep using the stale entry — so our shadow keeping the mapping alive during that window
is correct, not a bug. It does mean a lazily-resynced shadow can hold a mapping to a freed kernel
page for a while, which is a *security* consideration for a hostile guest but not a correctness one.

**Huge-page splits.** `huge_pmd_needs_flush()` mirrors `pte_needs_flush()` but does **not** ignore
`_PAGE_ACCESSED`, and `_PAGE_PSE` is in `flush_on_change`, so any change of page size is flushed.
More importantly, §5.10.4.2 recommends the clear-then-invalidate-then-set algorithm for size changes
(`C`): *"first clear the P flag in the relevant paging-structure entry (e.g., PDE); then invalidate
any translations for the affected linear addresses; and then modify the relevant paging-structure
entry to set the P flag and establish modified translation(s) for the new page size."*

**This is the same shape as ARM's break-before-make requirement**, and `FEAT_BBM = 0` on Apple
(`06 §2.4`) means we must do BBM ourselves. Since the corpus has already committed to shattering
everything to 4 KiB in the shadow (`14 §G-1a/b`, `16 §4`), a guest huge-page split changes no ARM
block size at all — it changes 512 already-4 KiB shadow entries. **Shattering buys us BBM-freedom
on the coherence path, which is a benefit `06 §2.4` noted for the fill path but not for this one.**

---

## 3 — The design space

### 3.1 Write-protect the guest PT pages and trap writes

**Correctness.** Complete and eager: the shadow is exact at every instant. It is the only scheme
that does not depend on the guest fulfilling any architectural obligation, so it is also the only
one robust to a *buggy or hostile* guest. It handles the COW RO→RW upgrade (§2.2) and the vmalloc
lazy purge without any special case.

**Cost.** Measured, §4: **33.3–34.3 ns** per guest PTE write when the shadow entry being replaced
was invalid, and **231–274 ns** when it was valid — and the latter is a **lower bound**, because
our handler stores the new descriptor and then invalidates, which is legal for a permission change
but not for an output-address change, where ARM requires full break-before-make (`FEAT_BBM = 0`,
`06 §2.4`) and a second `TLBI`.

**What guest behaviour breaks it.**

* **Linux writes its PTEs through the direct map**, `__va(pfn)`, which the kernel maps with 1 GiB
  and 2 MiB pages. Write-protecting a single 4 KiB PT page therefore requires the shadow to shatter
  that huge mapping — which we do anyway (`14 §G-1b` proved 4 KiB permissions are enforced inside
  one 16 KiB host page), but it costs TLB reach: `16 §4` measured **1.0–2.7×** on TLB-bound code,
  worst in the 16–24 MiB band.
* **Aliasing.** A PT page is reachable from the direct map, from `kmap` windows, and on Windows
  from the self-map. Revoking write access must clear the write bit in *every* shadow PTE that maps
  the frame, which needs a `GPA → {shadow PTE*}` reverse map. `06 §2.4` calls this "the single
  most-underestimated piece of shadow paging" and it is not costed anywhere, including here — the
  probe models a **single** shadow alias per frame (§6, UNRESOLVED).
* **Self-referencing page tables.** `06 §2.4` is right that this is a disaster for write-protection:
  every write through the self-map window faults, and the window's virtual aliases must be
  protected too (`03:583`).
* **`STP` and atomics.** Measured here: one `STP` fault writes **two** guest PTEs, and Linux uses
  atomic RMW (`cmpxchg`, `xchg`) on PTEs in `ptep_get_and_clear`, `ptep_set_wrprotect` and the
  A/D-race paths. A write-protected atomic cannot be emulated by decode-apply-`ERET`; it needs a
  real emulation of the RMW against the guest's memory model. **Not measured. Not modelled.**

### 3.2 Trap only on `INVLPG` / `MOV CR3` / `MOV CR4`, and resync lazily

**Correctness.** Sound, and §1 says why: the only transition the guest may perform silently is
P 0→1 (plus A 0→1, plus the three permission *upgrades*), and every one of those is caught by our
own fault handler on first use rather than by an announcement. Every transition that could produce
a *stale, over-permissive* shadow entry — P 1→0, R/W 1→0, U/S 1→0, XD 0→1, PFN change, D 1→0 — the
guest is architecturally obliged to announce, and if it does not, real hardware would misbehave too.

**Cost.** ~280 ns per handled `INVLPG` (drop shadow PTE + `TLBI`, `16 §3`), 10.3 ns per `MOV CR3`
that hits a cached shadow root (`16 §5`), plus the 146 ns shadow fills we pay regardless
(`16 §2`). Event rates measured in §5: **1.6–7.1 `INVLPG` per 10⁹ instructions** on compute,
**19 302 per 10⁹** in a fork storm.

**What guest behaviour breaks it.** A guest that violates its own architectural contract. Concretely:
a guest that clears a PTE and reuses the frame before invalidating; a guest that relies on
`CR4.SMAP`/`SMEP` 1→0 (which §1.3 shows is *not* a required-invalidation event, so we must flush on
our own initiative); a guest whose `INVLPG` we mis-map — see §6.2 on ranges and on `INVPCID`
type 2 vs 3. Also: **it is not robust to a hostile guest** in the way write-protection is. A guest
that deliberately never invalidates can keep a stale shadow mapping alive. Within a single guest's
own address space that is exactly what real hardware would do, so it is not an escape; it becomes a
concern only if shadow entries could ever outlive the guest-physical frame's ownership, which is a
constraint on our own frame-recycling code, not on the guest.

### 3.3 Para-virtualised PT updates (a guest-side shim)

**Correctness.** Perfect by construction. **Cost.** Near zero in traps; large in ecosystem.

**The corpus has already refuted this and the refutation holds.** `07 §3` records that Xen invented
PV-MMU, KVM never built one because EPT/NPT made it pointless, VMware's VMI PV-MMU was **built,
shipped, and deleted** from Linux 2.6.37, and Xen-on-ARM explicitly declined to build one ("no PV
MMU calls, no shadow pagetables"). `07 §3` also establishes the ABI cost precisely: everything a
PV-MMU needs — `set_pte`/`set_pmd`, CR2/CR3, alloc/release — is `PARAVIRT_XXL`, i.e. Xen-PV-only,
i.e. **a guest kernel fork**. `07 §7` recommendation 2 is "Do not build one."

**This document adds a quantitative reason rather than a precedential one.** PV-MMU would save the
write-protect cost, which we have just decided not to pay; against the *lazy* baseline it would save
**5.1 µs per `fork`+`exec`** and **2 parts per million** on compute workloads. There is nothing
there. What *is* worth taking is the one thing `07 §2.3` already identified as free: `pv_ops.mmu.
flush_tlb_multi` is unconditional under plain `CONFIG_PARAVIRT`, needs no kernel fork, and converts
a cross-vCPU shootdown IPI storm into a single call — relevant to §6.2, not to this document's
question.

### 3.4 Full re-walk on every `CR3` load

**Correctness.** Sound — it is the "rebuild everything" limit of lazy resync — but only if you also
handle `INVLPG` within a `CR3` epoch, because Linux changes PTEs constantly without touching `CR3`.
So it is not actually an alternative to §3.2; it is §3.2 with the shadow-root cache deleted.

**Cost.** Catastrophic, and now measured from two directions. Discarding the shadow on every `CR3`
load costs the refill: `16 §5` measured **20.4 ns/page**, capped at **~63 µs** for a
TLB-capacity-sized working set. At the measured **6.5 `MOV CR3` per `fork`+`exec`** and a busy
guest's 10 000–50 000 context switches/s, that is 0.2–3.2 ms/s of pure refill — 0.02–0.3 % — *if*
you only lose the TLB. If you rebuild the *shadow tables* rather than just the TLB you additionally
pay a 146 ns fill per touched page, which for a 12 MiB working set is **449 µs per `CR3` load**.
That is three orders of magnitude worse than the 10.3 ns an ASID switch costs. **Not viable.**

The measurement in §7 makes the point sharper still: with the guest's kernel half in `TTBR1` and
marked global, a `CR3` switch costs the kernel half **0.054 ns/page** — i.e. nothing. The entire
kernel half is exempt from `CR3`-driven rebuild, for free, and a full re-walk design throws that
away.

---

## 4 — Write-protection cost, measured

`probes/pt_coherence/pt_wp.c`. A "guest kernel" at ARM EL1 stores to a region of 512 4 KiB pages
that are mapped **read-only** in our own stage-1 tables (`AP[2]=1`) and simultaneously **writable**
at a second, private alias. That is exactly the write-protect design: the guest's view of its own
page tables is read-only, ours is not. The handler runs under our `VBAR_EL1` with no host exit.

### 4.1 What the syndrome tells us — and it is almost nothing

```
  a LOAD from the write-protected page took 0 fault(s) and returned 0x0
  faults taken by the five stores: 5 (expected 5)

  STR  Xt, [Xn]      (8-byte store)
    ESR_EL1 = 0x000000009600004f   EC=0x25  ISV=0  SAS=0  SSE=0  SRT=0  SF=0  AR=0
    WnR=1  CM=0  S1PTW=0  DFSC=0x0f   FAR_EL1=0x400000000000
    ELR_EL1=0x4000019c  instruction word at ELR = 0xf9000005  (Rt field = 5)
  STRB Wt, [Xn]      (1-byte store)          ESR_EL1 = 0x000000009600004f  ... 0x39000005
  STR  Wt, [Xn]      (4-byte store)          ESR_EL1 = 0x000000009600004f  ... 0xb9000005
  STP  Xt, Xt2, [Xn] (16-byte pair store)    ESR_EL1 = 0x000000009600004f  ... 0xa9001805
  STUR Xt, [Xn, #1]  (unaligned store)       ESR_EL1 = 0x000000009600004f  FAR_EL1=0x400000000001
```

**Four things fall out, and two of them were not expected:**

1. **`ESR_EL1` is bit-for-bit identical — `0x9600004f` — for all five store forms.** `ISV = 0`,
   so `SAS` (size), `SRT` (transfer register), `SF` and `AR` are all zero and meaningless.
   The syndrome distinguishes *nothing* about the access except that it was a write
   (`WnR=1`) that failed a level-3 permission check (`DFSC=0x0f`). ARM populates `ISV` only for a
   restricted class of aborts — in practice stage-2 aborts on simple loads/stores — and a stage-1
   permission fault at the current EL is not in it. **The handler must decode the instruction
   itself.** (`M`)
2. **`FAR_EL1` is exact, including for the unaligned store** (`0x400000000001`). So the *address*
   is free; only the *data* and *width* require decode.
3. **A read of a write-protected page takes zero faults.** `AP[2]=1` at EL1 is read-only, not
   no-access, so the guest kernel's own page-table *walks* and reads — `pte_none()`, page reclaim
   scanning, `/proc/pid/pagemap` — cost nothing under write-protection. This is a genuine advantage
   of write-protection over any scheme that unmaps.
4. **`STP` writes two guest PTEs from one fault.** A decoder that extracts only `Rt` and assumes an
   8-byte store silently corrupts the second PTE. Linux's `set_ptes()` on a multi-page folio is
   compiled into exactly such stores.

### 4.2 The cost ladder

Two passes over an identical configuration list; pass 1 shows the positional artifact described in
*Method*, pass 2 is the measurement. `NET` is against a matched control loop over a writable region
of identical geometry. Every arm took exactly 51 712 faults and, from step 3 onward, was verified
host-side to have applied all 512 PTE writes correctly.

```
  -- pattern: 512 consecutive PTE slots in ONE PT page (fork-like)
     control: same loop, writable region (pass 1 / pass 2)     0.41     0.33  ns/store
                                                             pass1    pass2    NET(p2)      cyc
     0  bare permission fault + ERET                         26.86    27.59      27.26       88
     1  + read ESR/FAR, check DFSC + WnR                     27.02    27.43      27.10       87
     2  + fetch faulting insn, decode Rt, read guest reg     29.13    28.32      27.99       90
     3  + apply the store to the guest PT page               29.13    28.32      27.99       90
     4  + rmap lookup PT frame -> shadow L3                  28.40    28.32      27.99       90
     5  + synthesise + store the shadow descriptor           33.85    34.67      34.34      111
     6  + DSB/TLBI VAAE1IS/DSB/ISB                          310.38   231.53     231.20      745
     0' bare permission fault again (drift check)            27.59    26.86      26.53       86

  -- pattern: one PTE slot in each of 512 PT pages (scattered)
     control: same loop, writable region (pass 1 / pass 2)     8.63     8.87  ns/store
     0  bare permission fault + ERET                        100.02    26.86      17.99       58
     2  + fetch faulting insn, decode Rt, read guest reg    107.42    29.22      20.35       66
     5  + synthesise + store the shadow descriptor           43.38    45.49      36.62      118
     6  + DSB/TLBI VAAE1IS/DSB/ISB                          240.32   277.02     268.15      864
```

Across four independent process invocations, step 5 landed at **33.3, 33.4, 34.3, 33.3 ns** (one
page) and step 6 at **264, 274, 269, 231 ns**.

**Three things fall out:**

1. **Step 0 independently replicates `16 §1`.** 25.3–27.3 ns here against 28.2 ns there, measured
   by a different probe with a different control. `16`'s permission-fault floor is confirmed.
2. **Steps 1–4 are free.** Reading two system registers, fetching and decoding the faulting
   instruction, reading the guest's register value out of a spill frame, applying the store, and
   doing a hashed rmap lookup with a tag check together cost **under 1 ns**. Steps 3 and 4 are
   *below the measurement resolution* (0.08 ns/store) — the work disappears into the shadow of the
   exception round trip, which the core has to drain anyway. **Optimising the handler's logic is
   pointless; the only levers are taking fewer faults and not invalidating.** This is the same
   conclusion `16 §2`'s "the dominant term is not the walk" reached on the fill path.
3. **The `TLBI` is 6.7× the entire rest of the handler.** 231 ns added to a 34 ns handler.
   `16`'s design rule — never invalidate when turning an invalid descriptor valid — therefore
   applies to this path with even more force than to the fill path, because here the invalid→valid
   case (an *install*) is the common one and the valid→valid case (a *modify*) is what we must
   detect and special-case.

*A methodological note worth recording:* the scattered pattern's `NET` figures are **lower** than
the single-page pattern's (18.0 vs 27.3 ns for step 0) even though its absolute figures are the
same. That is not a real effect: the scattered *control* costs 8.87 ns/store because it takes 512
distinct cache and TLB misses that the faulting arm — whose store never executes — never pays. The
NET is a fair subtraction only when the control's misses are the same misses the measured arm
would pay, and here they are not. **For the scattered pattern the absolute figures (26.9 / 45.5 /
277.0 ns) are the honest ones, not the NET.**

### 4.3 Burst behaviour: trapping never amortises

The classic mitigation is to stop trapping: on the first fault, make the PT page writable, let the
guest run, then re-protect and re-derive the whole page's shadow. Measured against trap-every-store,
with the trap arm using the *cheap* (install, no per-fault `TLBI`) handler:

```
       N     M     trap-each     flip+TLBI   flip noTLBI  native floor
                  ns/episode    ns/episode    ns/episode    ns/episode
       1 16384          33.6        1137.8         837.5           0.3
       4  4096         136.2        1148.4         801.2           1.8
      16  1024         559.6        1082.5         812.0           5.7
      32   512        1086.5        1141.4         829.3          10.4
      64   256        2163.2        1100.6         800.1          23.8
     256    64        8620.4        1194.0         927.1          84.6
     512    32       17747.4        1507.8        1296.9         162.8

  per-store:   N=1      33.64   1137.76    837.46
               N=32     33.95     35.67     25.91
               N=512    34.66      2.94      2.53
```

**Two things fall out, and one of them kills a plausible optimisation:**

1. **Trap-each is perfectly flat at 33.6–34.7 ns per store across three orders of magnitude of N.**
   There is no batching to exploit and no warm-up to amortise: every store pays its own exception.
   A "coalesce adjacent PTE writes" optimisation has nothing to work with, because the coalescing
   would have to happen *before* the fault, and the fault is the notification.
2. **The flip episode is flat too, at ~830 ns (install case) or ~1130 ns (modify case), regardless
   of N.** It decomposes cleanly against `16 §3`: two protection flips at ~280 ns each = 560 ns,
   plus a 512-entry shadow re-derivation at the 5.13 ns/entry `16 §2` measured for
   walk+translate+store ≈ 260 ns, plus (in the `+TLBI` arm) one `TLBI VMALLE1IS` — and the measured
   gap between the two flip arms is **300 ns**, against `16 §3`'s independently measured
   **321 ns** for `TLBI VMALLE1IS`. The pieces add up.
3. **Break-even is N ≈ 32 (install) / N ≈ 64 (modify).** Below that, trapping every store is
   cheaper; above it, unprotect-and-resync wins, asymptotically by 12×. **`fork` copying a full
   512-entry PT page is above the line; essentially everything else a kernel does to a page table
   is below it.** A real implementation would need an adaptive per-page policy — which is
   implementable, but it is a second mechanism layered on a first one we have already concluded is
   the wrong choice.

---

## 5 — How often does this actually happen? Measured event rates, inferred write rates

`probes/pt_coherence/qemu/`. A Linux **6.18.35** x86-64 guest (Alpine `virt` kernel, busybox
initramfs) under QEMU TCG on this machine, instrumented with a TCG plugin that counts
invalidation-class instructions inline, and with `/proc/vmstat` + `/proc/stat` snapshots taken
inside the guest immediately before and after the workload. **All figures below are deltas against
a matched `task=none` boot of the identical image**, so boot activity is subtracted.

| Workload | Δinsns | `INVLPG` | `MOV CR3` | `INVLPG` per 10⁹ insn | Tag |
|---|---|---|---|---|---|
| 2000 × `fork`+`exec` (busybox `true`), smp1 | 1.88 G | **36 259** | 12 953 | **19 302** | `M` |
| 2000 × `fork`+`exec`, smp4 | 1.97 G | 36 369 | 10 248 | 18 504 | `M` |
| 7-Zip `b -mmt1`, smp1 | 32.97 G | **235** | 36 | **7.1** | `M` |
| 7-Zip `b -mmt4`, smp4 | 117.97 G | 450 | 123 | **3.8** | `M` |
| ALU/mem/matmul/nqueens suite, smp1 | 42.61 G | **68** | 50 | **1.6** | `M` |

Per `fork`+`exec` of a static binary (`M`): **939 k guest instructions, 18.1 `INVLPG`, 6.5 `MOV
CR3`, 59.3 guest page faults, 464 548 guest memory writes.**

### 5.1 The arithmetic, with the inferred quantity clearly marked

**What is MEASURED:** the `INVLPG` count, the `MOV CR3` count, the page-fault count, the instruction
count, and the total store count.

**What is INFERRED:** the number of *page-table* writes. Nothing in this harness identifies which
stores land on a page-table page. Building the range from the measured quantities:

* Each of the **59.3** measured page faults installs at least one PTE. `M` → ≥ 59 writes.
* `fork` copies the parent's leaf PTEs. The parent is busybox `ash`; a static busybox has an RSS on
  the order of 200–500 4 KiB pages, so `copy_page_range` performs **200–500** PTE writes plus PT
  page allocation and zeroing. `INFERRED`.
* `exec` tears down the forked address space (`unmap`, another 200–500 PTE clears) and builds a new
  one; `exit` tears that down. `INFERRED`.
* Total: **500–2000 PTE writes per `fork`+`exec`**. `INFERRED`.
* Sanity check against a measured quantity: 500–2000 out of 464 548 total guest stores is
  **0.1 %–0.4 %** of all stores, which is a plausible fraction for the most PT-intensive workload
  in the set and would be implausible if it were an order of magnitude either way.

**Cost, per `fork`+`exec`:**

| Design | Arithmetic | Cost | Tag |
|---|---|---|---|
| Write-protect, install case | 500–2000 × 33.4 ns | **16.7 – 66.8 µs** | `X` on `M`+`INFERRED` |
| Write-protect, modify case | 500–2000 × 270 ns | 135 – 540 µs | `X` |
| Lazy resync, invalidation work | 18.1 × 280 ns + 6.5 × 10.3 ns | **5.1 µs** | `X` on `M` |
| (both also pay) shadow fills | ≈ 59–400 × 146 ns | 8.7 – 58 µs | `X` |

A `fork`+`exec` is 939 k guest instructions; at an optimistic 1 GIPS of translated throughput that
is ~0.94 ms of guest time. So write-protection adds **1.8 %–7.1 %** in its best case and
**14 %–57 %** whenever the PTE being written replaces a live mapping; lazy resync adds **0.54 %**.

**On compute workloads the comparison is not close at all.** At 1.6–7.1 `INVLPG` per 10⁹
instructions, lazy resync costs **0.4–2.0 µs per 10⁹ instructions** — about **2 parts per million**.
Write-protection's cost on the same workloads is unknown but strictly positive, and 7-Zip's
2.4 G stores per 33 G instructions give it plenty of room to be much larger.

*Two caveats on this section, both material:*

* **These are per-instruction and per-operation rates, not per-second rates.** They are taken under
  QEMU TCG, which runs the guest 10–50× slower than native, so the guest's *wall-clock* event rate
  under emulation is not the rate natemu would see. Normalising per guest instruction and per
  `fork` removes that dependence, which is why the table is presented that way.
* **An unexplained observation, recorded rather than smoothed.** A `task=none` boot of the same
  image issues **20 009 `MOV CR3`** with `-smp 1` and **460** with `-smp 4`, at nearly identical
  instruction counts (1.12 G vs 1.25 G) and nearly identical `iret`/`swapgs` counts. A 43×
  difference in `CR3` writes with no corresponding difference in anything else. The workload deltas
  are consistent between the two (6.5 vs 5.1 per spawn), so this is confined to boot. The most
  plausible explanation is `text_poke`'s use of a temporary `mm` (`use_temporary_mm()` writes `CR3`
  twice per poke) taking a different, batched path under SMP — but that is a guess.
  **UNRESOLVED.** *Experiment that would settle it:* re-run both with `-d plugin` plus a plugin that
  records the guest RIP of each `CR3` write and resolve those against `/proc/kallsyms` from the same
  boot.

---

## 6 — SMP, and the interaction with 256 ASIDs

### 6.1 Another vCPU walking the shadow while we rebuild it

Under any lazy scheme, vCPU A repairs a shadow entry while vCPU B's *hardware* page-table walker may
be reading the same descriptor. Three facts bound the problem:

1. **Descriptor updates are single aligned 64-bit stores and are therefore atomic.** A walker sees
   either the old or the new descriptor, never a torn one. This holds for install
   (invalid→valid) and for permission-only changes.
2. **It does not hold for output-address changes.** `FEAT_BBM = 0` on Apple (`06 §2.4`), so changing
   a valid descriptor's output address, block size or memory type requires the full
   invalid → `DSB` → `TLBI` → `DSB` → new → `DSB`/`ISB` sequence, or the architecture permits a TLB
   conflict abort. **The 231–274 ns measured in §4.2 for the modify case does *not* include a BBM
   sequence** — the handler stores the new descriptor and then invalidates. So §4.2 step 6 is a
   **lower bound** for output-address changes, short by roughly one more `TLBI` + barriers
   (~280 ns, `16 §3`). This makes write-protection's modify case worse than measured, not better.
3. **Broadcast invalidation is not the problem.** `16 §3` measured `TLBI VAE1` (non-shareable)
   and `TLBI VAE1IS` (inner-shareable broadcast) within 2 % of each other, and only
   **+4 % to +18 %** with one contending vCPU. `19` measured 92 % scaling efficiency at 8 vCPUs for
   shadow fills, and — decisively — **45 M fills/s aggregate without a per-fill `TLBI` versus
   6.6 M/s with one, a 6.8× difference.** Whatever we do on the coherence path, it must not put a
   `TLBI` in the common case.

### 6.2 Mapping x86's invalidation semantics onto ARM's

x86's `INVLPG` is **core-local**; Linux does cross-CPU shootdown in software via IPIs. ARM's
`TLBI VAE1IS` is **hardware-broadcast** (`03:577`). §1.5 established that x86 permits the processor
to invalidate *more* than required, so mapping a core-local `INVLPG` onto a broadcast `TLBI` is
an over-approximation and is safe. **It is also an opportunity**: a guest's software shootdown IPI
sequence can be collapsed, because the first vCPU's `TLBI VAE1IS` has already done the work the
IPIs were sent to request. Combined with `pv_ops.mmu.flush_tlb_multi` — which `07 §2.3` records as
free and upstream, no kernel fork — the shootdown IPIs can be elided entirely.

Two mappings need care, and both follow from §7's measurement:

* **A guest `INVLPG` on a kernel-half address must use `TLBI VAAE1IS`, not `VAE1IS`.** With the
  kernel half in `TTBR1` and marked global (`nG=0`), its entries are not ASID-tagged; §7 measured
  that `TLBI ASIDE1IS` does **not** evict them. `VAE1IS` is ASID-qualified and would miss.
* **`INVPCID` type 2 ("including globals") must map to `TLBI VMALLE1IS`, and type 3 must not.**
  Type 3 excludes globals; §7 measured that `TLBI VMALLE1IS` costs the kernel half a full refill
  (+21.1 ns/page, ~63 µs for a 12 MiB working set) while `ASIDE1IS` costs it nothing. Using the
  wrong one is not a correctness bug — over-invalidation is legal — but it is a **60 µs** mistake
  each time.

### 6.3 The 256-ASID ceiling

`14` measured `ID_AA64MMFR0_EL1.ASIDBits = 0` → **256 ASIDs**, against x86's 4096 PCIDs, and
`16 §5` already refuted `09` N-11's alarm: a rollover costs at most ~63 µs and models to
**< 0.25 % of a core**, because the flush destroys only what fits in a 3072-entry TLB and the
incoming address space had to refill anyway.

**This document adds the reason the ceiling matters even less than that.** §7 measures that the
guest's *kernel half*, in `TTBR1` with `nG=0`, is entirely outside the ASID mechanism: it survives
a `TTBR0` write with a brand-new ASID at **+0.054 ns/page**, survives 64 consecutive fresh-ASID
`TTBR0` writes with zero faults, and is not evicted by `TLBI ASIDE1IS`. Since a Linux guest's kernel
half is process-invariant and is a large fraction of the resident TLB footprint of any
kernel-intensive workload, **ASID pressure applies only to the user half**, and an ASID rollover
destroys only user-half entries. `16 §5`'s already-small figure is therefore an over-estimate.

> ⚠️ **One correctness trap, found by this probe failing.** The first run of `ttbr1_split` mapped the
> user-half page with `nG=0`. It then **kept resolving after `TTBR0` was pointed at an all-zero
> root** — a global entry is not ASID-tagged and is not evicted by a `TTBR0` write. Four of the
> seventeen checks failed and the failure mode was a *stale user mapping surviving a guest `CR3`
> switch*, which in a real emulator is a cross-process information leak. **Every shadow entry for
> the guest's user half must be `nG=1`, and every entry for the kernel half should be `nG=0`.
> Getting this backwards is silent, and neither the ARM ARM nor any prior doc in this corpus flags
> it.**

---

## 7 — The `TTBR1` kernel half: executed, not asserted

`probes/pt_coherence/ttbr1_split.c` was found half-written in the tree, never built. It has been
finished (three unresolved label/offset bugs and one missing symbol export) and run. It builds a
real `T0SZ = T1SZ = 17` pair of trees and runs guest EL1 code, its exception vectors, its data and
its scratch block **entirely out of `TTBR1` at the actual VAs Linux uses** —
`0xffffffff81000000` for kernel text, `0xffff888000001000` in the direct map.

```
  TCR_EL1  = 0x2b5113511  (T0SZ=17 T1SZ=17 TG0=0 TG1=2 EPD1=0 A1=0)
  [PASS] guest executed instructions fetched through TTBR1 at 0xffffffff81xxxxxx
  [PASS] TCR_EL1 accepted T0SZ=T1SZ=17 (split at bit 47 == x86 canonical boundary)
  [PASS] TTBR1 is enabled (EPD1=0)
  [PASS] store+load through TTBR1 at Linux direct-map VA 0xffff888000001000
  [PASS] kernel half unchanged after TTBR0 swapped to a DIFFERENT tree (no TLBI issued)
  [PASS] user-half VA faulted exactly once under the new TTBR0
  [PASS] that fault was a translation fault with FAR == the user-half VA
  [PASS] kernel half unchanged with TTBR0 pointing at an ALL-ZERO root
  [PASS] a subroutine call+return between two TTBR1 VAs works with TTBR0 mapping nothing
  [PASS] kernel half survives 64 back-to-back TTBR0 writes with 64 different ASIDs
  [PASS] restoring the original TTBR0 restores the user-half mapping and its data
  [PASS] exactly two faults in the whole run, both the intended ones
  ... 17/17 PASS
```

and the cost of a guest `CR3` switch to the kernel half, 3072 pages (12 MiB) per arm, three disjoint
physical regions:

```
  arm                                           ns/access     ns/round
  TTBR1 global      chase, warm                     7.663      23541.7
  TTBR1 global      chase, fresh-ASID TTBR0         7.718      23708.3
  TTBR1 non-global  chase, warm                    11.570      35541.7
  TTBR1 non-global  chase, fresh-ASID TTBR0        31.277      96083.3
  TTBR0             chase, warm                     9.644      29625.0
  TTBR0             chase, fresh-ASID TTBR0        30.368      93291.7
  TTBR1 global      chase, TLBI ASIDE1IS            8.124      24958.3
  TTBR1 global      chase, TLBI VMALLE1IS          28.768      88375.0

    TTBR1 global, refill charged by a fresh-ASID TTBR0 write :   +0.054 ns/page
    TTBR1 non-global, same                                   :  +19.708 ns/page
    TTBR0,            same                                   :  +20.725 ns/page
    TTBR1 global, after TLBI ASIDE1IS                        :   +0.461 ns/page
    TTBR1 global, after TLBI VMALLE1IS                       :  +21.105 ns/page

    whole-working-set cost of one CR3 switch, kernel half global     :      0.2 us
    whole-working-set cost of one CR3 switch, kernel half non-global :     60.5 us
```

**Three things fall out:**

1. **`T0SZ = T1SZ = 17` works under HVF**, giving 47-bit halves and a split at exactly bit 47 —
   x86-64's canonical boundary, exactly. `14 §G-1a` had only ever exercised `TTBR0` with
   `T0SZ = 16` and `EPD1 = 1`; `T1SZ` had never been written anywhere in this corpus. It is now
   closed. (Note that `T0SZ = 16` also works and is what `14` measured; the difference is that at
   16 the ARM split is at bit 48 and the ARM-legal range is a strict *superset* of x86's canonical
   range, which is harmless because every canonical x86 address still lands in the correct TTBR.
   17 is the exact fit; 16 is the safe fit. Either is usable.)
2. **The kernel half is free across `CR3` switches, and the number is 0.054 ns/page — i.e. nothing.**
   The same working set in `TTBR0` pays 20.7 ns/page, replicating `16 §5`'s 20.4 ns/page from a
   different probe. `06 §2.4`'s "makes CR3 switches genuinely cheap" is confirmed, and the
   magnitude is now known: **380× cheaper than leaving the kernel half in `TTBR0`.**
3. **`TLBI ASIDE1IS` does not evict global entries and `TLBI VMALLE1IS` does**, at 0.46 vs
   21.1 ns/page. This is what the architecture says and it is now verified on this hardware, which
   matters because §6.2's `INVPCID` type-2-vs-3 mapping depends on it.

**The caveat `06 §2.4` raised survives and is now more important.** The guest's high-half PML4
entries live in the *same* PML4 page as its low-half entries, so a guest write there must be
reflected into the `TTBR1` shadow as well as the `TTBR0` one. Under the recommended lazy design this
is not a "special case in the write-protect handler" as `06` framed it — there is no write-protect
handler — it is a special case in the **`INVLPG`/`CR3` resync path**: an invalidation whose address
falls in the high half must repair the `TTBR1` tree and use `VAAE1IS`. **This is the strongest
remaining argument for write-protecting the PML4 page specifically**, which §0.5 already recommends.

---

## Implications for the Tier 2 design

### The design rules that follow

1. **Guest page tables stay writable. Resync lazily.** Driven by `INVLPG`, `INVPCID`, `MOV CR3`,
   `MOV CR4`, `MOV CR0`, and by our own shadow-miss and permission faults.
2. **Write-protect the top level only.** The PML4 page changes rarely, each change can invalidate a
   whole shadow root, and it is the page whose high-half entries must be mirrored into `TTBR1`
   (§7). At 33.4 ns a write and a handful of writes per address-space creation, this is free.
3. **Keep shadow entries present-but-restricted, never invalid, wherever a choice exists.**
   Confirmed a third time here: a permission fault is 27 ns, a translation fault is 94 ns
   (`16 §1`). Our repair trigger for the COW RO→RW upgrade (§2.2) *is* a permission fault, and it
   is the cheapest fault the machine has.
4. **Never issue a `TLBI` when turning an invalid descriptor valid.** `16`'s rule, re-confirmed on
   a second path: it is worth 6.7× on the write-protect handler (34 → 265 ns) and 3.0× on the fill
   handler.
5. **Kernel half in `TTBR1` with `nG=0`; user half in `TTBR0` with `nG=1`.** Non-negotiable in both
   directions — the first buys a 380× cheaper `CR3` switch, the second prevents a stale user
   mapping surviving one (§6.3).
6. **Map a guest `INVLPG` on a high-half address to `TLBI VAAE1IS`, not `VAE1IS`; map `INVPCID`
   type 2 to `VMALLE1IS` and type 3 to `ASIDE1IS`.** Measured cost of confusing the last pair:
   60 µs per occurrence.
7. **Take `pv_ops.mmu.flush_tlb_multi`.** Upstream, unconditional under `CONFIG_PARAVIRT`, no guest
   fork (`07 §2.3`). It is the only PV-MMU-adjacent thing worth having, and it addresses SMP
   shootdown, not page-table coherence.
8. **Do not build a PV-MMU.** `07 §7` already said so on precedential grounds; §5 now says so on
   quantitative ones — it would save 5.1 µs per `fork` and 2 ppm on compute.

### The honest summary

> Page-table coherence turned out not to be a hard problem, and the reason is a single sentence in
> the SDM plus a single measured ratio. The sentence — a P 0→1 transition needs no invalidation
> because such an entry is never cached — means a lazy scheme can never be *surprised* by a new
> mapping. The ratio — 18 `INVLPG` against 500–2000 PTE writes per `fork` — means the announcement
> channel carries 30–110× less traffic than the store channel for the same information. Everything
> else in this document is bookkeeping around those two facts.
>
> The one thing that did not survive contact with the SDM is `06 §2.4`'s stated *reason* for lazy
> resync being safe. It is not that the architecture forbids un-announced permission upgrades — it
> explicitly permits them, and Linux performs one on every COW fault. It is that our own permission
> fault costs 27 ns and catches them.

---

## Corrections and additions to the corpus

* **`06 §2.4` "the architecture forbids the guest from doing [a present→present permission upgrade
  without invalidation], so it is safe" is wrong.** SDM §5.10.4.3 explicitly permits it for
  `R/W` 0→1, `U/S` 0→1 (when `CR4.SMEP=0`) and `XD` 1→0, specifying a spurious `#PF` as the only
  consequence, and Linux's `ptep_set_access_flags`/`wp_page_reuse` COW path relies on it. **The
  conclusion — lazy resync is safe — survives, but the mechanism is our own permission fault, not a
  guest prohibition.**
* **`06 §2.4` "This is the right default" for lazy resync is confirmed, and now quantified**: 30–110×
  fewer events than write-protection, at 8.4× the per-event cost.
* **`06 §2.4` "write-protect only the *top* level (PML4), which changes rarely" is confirmed and
  promoted** from a hybrid suggestion to the recommendation, and §7 supplies a second reason for it
  (the shared low/high PML4 page).
* **`06 §2.2`'s "PCID (12-bit, 4096) | ASID (16-bit, 65536 — `FEAT_ASID16`) | Superset ✅" remains
  uncorrected in `06` and is wrong.** Measured `ASIDBits = 0` → **256** ASIDs (`13 §A-1`, `14`).
  `06 §2.4`'s "With `FEAT_ASID16` (65 536 ASIDs) we never need to flush on a context switch"
  inherits the error. Doc 20 does not rely on either.
* **`06 §2.6`'s list of whole-shadow invalidators is right in outcome, wrong in premise.**
  `CR4.PSE`, `CR4.SMEP` 1→0 and `CR4.SMAP` are *not* required-invalidation events per SDM
  §5.10.4.1 — the guest is not obliged to announce them, so we must flush on our own initiative
  when we observe the `CR4` write, which is what `06` recommends anyway.
* **`06 §2.1`'s "the processor is forbidden from caching a translation for a non-present page" is a
  paraphrase, not a quote.** The SDM states a necessary condition, never that affirmative sentence.
  The quotable form is §5.10.4.3's first bullet.
* **SDM section numbering corpus-wide:** paging is Chapter **5**, not 4. `06 §2.1`'s "§4.10",
  `06 §2.5`'s "§4.8" and Linux's own `fault.c` comment all use the old numbering.
* **`09` N-11 (256 ASIDs) should be downgraded further than `16 §5` already downgraded it.**
  With the kernel half global in `TTBR1`, ASID pressure applies to the user half only.
* **`12` has no ranking item for page-table coherence and `INVLPG` appears nowhere in it.** This
  document supplies the missing cost model (§0.3) and the missing event rates (§5). On those
  numbers, coherence would rank **below** rank 8 (A/D + TLB reach) under the recommended design and
  **above rank 1** under the write-protect design — i.e. the ranking is entirely a function of the
  choice, which is why it could not be ranked before.
* **`16 §1`'s 28.2 ns permission fault is independently replicated** at 25.3–27.3 ns by a different
  probe with a different control.
* **`16 §3`'s 321 ns `TLBI VMALLE1IS` is independently replicated** as the 300 ns gap between the
  two flip arms in §4.3.
* **`16 §5`'s 20.4 ns/page fresh-ASID refill is independently replicated** at 20.7 ns/page in §7.
* **New: `ESR_EL1.ISV = 0` for stage-1 permission faults at guest EL1**, verified across five store
  encodings. Any guest-store-trapping design in this corpus — write-protection here, SMC detection
  in `06 §8` — must budget for a software instruction decoder. `02`, `10:130` anticipated an
  "`ISV=0` decoder"; this confirms it is the only case, not a corner case.
* **New: `T0SZ = T1SZ = 17` with `EPD1 = 0` works under HVF**, closing the corpus's only untested
  `TCR_EL1` field. The raw-report note that the corpus says `16` rather than `17` is correct —
  both work; 17 is the exact canonical fit and 16 is a superset.
* **New: shadow entries for the guest's user half must be `nG=1`.** An `nG=0` user mapping survives
  a `TTBR0` write with a different ASID, producing a stale cross-process mapping. Found by a probe
  failure, not by reasoning.

---

## What is still open

* **The guest PT-write rate is INFERRED, not measured, and it is the one number the recommendation's
  margin depends on.** *Experiment that would settle it:* extend `plugin_inv.c` to resolve each
  store's guest-physical address (`qemu_plugin_hwaddr_phys_addr`) against a set of page-table frames
  discovered by walking from `CR3` — readable from the plugin via
  `qemu_plugin_read_memory_vaddr` at the kernel direct map, with `nokaslr` to fix
  `page_offset_base`. The frame set must be re-derived periodically since it changes constantly.
  This is a day of work and would replace the 500–2000 range with a measurement.
* **Atomic RMW on a write-protected page is not modelled at all.** Linux uses `cmpxchg`/`xchg` on
  PTEs in `ptep_get_and_clear`, `ptep_set_wrprotect` and the A/D-race paths. Trapping a
  `LDXR`/`STXR` pair or a `CAS` and emulating it correctly against the guest's memory model is
  materially harder than trapping a plain store, and none of §4's numbers apply to it. It is a
  further argument against write-protection rather than a gap in the recommendation, but it is a
  gap in the measurement. **UNRESOLVED.**
* **The rmap fan-out is not measured.** `pt_wp.c` models exactly one shadow alias per guest PT frame
  and its rmap lookup is a verified single-probe hit. A real `GPA → {shadow PTE*}` map with shared
  address spaces (`MAP_SHARED`, shared libraries, `11 §2`) has a fan-out of unknown distribution,
  and `06 §2.4` calls it "the single most-underestimated piece of shadow paging". Still uncosted.
* **The modify-case figure of 231–274 ns is a lower bound.** It omits break-before-make, which
  `FEAT_BBM = 0` makes mandatory for output-address changes and which costs roughly one more
  `TLBI` + barriers (~280 ns). Measuring a correct BBM sequence would strengthen the recommendation,
  not weaken it.
* **The `-smp 1` versus `-smp 4` boot-time `CR3`-write discrepancy** (20 009 vs 460) is
  **UNRESOLVED**; see §5.1 for the experiment that would settle it.
* **Multi-vCPU coherence was not measured here at all.** `16 §3`'s contention figures are one
  contending vCPU; `19`'s scaling numbers are for fills, not invalidations. The specific hazard of
  one vCPU repairing a shadow entry while another's hardware walker reads it was reasoned about
  (§6.1) and not tested. *Experiment:* two vCPUs, one repeatedly flipping a descriptor between two
  valid output addresses with and without BBM, the other chasing through it, checking for TLB
  conflict aborts and for stale reads.
* **Everything here is one machine (M2 Max), one OS (macOS 26.5.2), one guest kernel (Linux
  6.18.35 / 6.12 for the source quotes), one QEMU vintage.** `13 §D-4` demands this caveat be
  corpus-wide; it applies in full. The clock estimate of 3.22 GHz is below the M2 Max P-core
  maximum, so cycle columns carry ~5 % uncertainty.

**Reproduce:**
`cd probes && ./build.sh pt_coherence/pt_wp.c && ./pt_coherence/bin/pt_wp all`
(individual experiments: `cal`, `isv`, `wp`, `burst`)
`cd probes && ./build.sh pt_coherence/ttbr1_split.c && ./pt_coherence/bin/ttbr1_split both`
(individual: `func`, `time`)
`probes/pt_coherence/qemu/sweep.sh p` (plugin counts) and `sweep.sh n` (wall-clock + `/proc` deltas).
