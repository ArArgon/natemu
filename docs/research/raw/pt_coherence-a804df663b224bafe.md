I'll start by surveying the corpus structure, then read the relevant docs in full.
Let me read the key docs in full, in parallel.
I read the full text of docs 06, 07, 09, 11, 12, 13, 14, 16, 17, plus the relevant sections of 00, 03, 15, 19. Findings below, organized to your (A)–(H) plus the style skeleton.

---

# ⚠️ First, a correction you need before you write doc 20

**The claim "with T0SZ=T1SZ=17 the ARM TTBR0/TTBR1 split lands exactly on x86's canonical boundary at bit 47" does not exist in this corpus in that form.** The corpus says **16**, not 17, in all three places it appears. Grep for `T1SZ` returns exactly three hits corpus-wide, all `=16`:

- `06-x86-system-semantics.md:19` (§0.1 table row): `| VA bits | 48 (canonical, sign-extended from bit 47) | 48 (`T0SZ`/`T1SZ` = 16) |`
- `06-x86-system-semantics.md:136` (§2.2 table row): `| Canonical split @ bit 47 | `TTBR0_EL1` / `TTBR1_EL1`, `T0SZ=T1SZ=16` | **Exact** |`
- `08-feasibility-synthesis.md:47`: *"Inside a VM we programme `TCR_EL1.T0SZ/T1SZ=16` and get the full 48-bit space."*

The prose form of the claim is `06 §0.1`, final sentence:

> "The x86 canonical-address split lands exactly on the `TTBR0`/`TTBR1` boundary."

And the strongest/longest statement is **`03-prior-art-hw-accel-dbt.md:808` (§7, "(a) The 64-bit-on-64-bit address-space problem largely evaporates")**:

> "On ARM64, **`TTBR0_EL1` covers the low VA range and `TTBR1_EL1` covers the high VA range, selected by the top address bits in hardware** — and x86-64's canonical split (low half `0x0000_0000_0000_0000`–`0x0000_7FFF_FFFF_FFFF`, high half `0xFFFF_8000_...`) lines up with ARM64's `T0SZ=T1SZ=16` split almost exactly. A guest x86-64 user address lands in TTBR0 territory and a guest x86-64 kernel address lands in TTBR1 territory **with no masking, no range check, and no table switch**. This is a materially better structural fit than Captive had, and it is arguably the strongest technical argument for this project's viability."

Note `03` hedges with **"almost exactly"** while `06 §2.2` grades it **"Exact"**. `T0SZ=16` (48-bit VA) is the *measured* value — `14 §G-1a` closed it: `TCR_EL1 = 0x280903510` (`T0SZ = 16`), test VA `0x0000_4000_0000_0000`. `T1SZ` has **never been tested** anywhere in the corpus; only `TTBR0_EL1` was written in the G-1a probe (`14:18`). That is an open item you can legitimately claim in doc 20.

---

# (A) `07-paravirtualization-design-space.md` — the full Xen PV-MMU argument

**Doc-level context first:** `07` carries a **RETRACTION NOTICE** at the top (lines 3–13) which explicitly preserves the PV-MMU argument:

> "Everything downstream of that premise is therefore void, in particular **§3.1 and §6.4's `natemu-page-quads` recommendation**… The rest of the document — **the analysis of classical PV being near-worthless to a DBT**, the Xen/KVM/Hyper-V survey, and the three-tier scope model — **is unaffected**."

So the material you need in doc 20 is the *surviving* part of `07`.

### (i) Write-protecting guest page tables

`07` itself does not analyse write-protection as a PV mechanism directly — it treats it as the *thing Xen PV-MMU removes*. Its §2.3 table row is:

> `| **PV-MMU (Xen-style: read-only guest PTs + `HYPERVISOR_mmu_update`)** | Shadow page-table maintenance and trap-on-PTE-write | Historically large — this was Xen's headline trick | **Small-to-moderate, and only if you already use shadow/host page tables.** It removes the *invalidation* half of the cost, never the *per-access* half. If you use a softmmu, the per-access cost dominates and PV-MMU does nothing for it | Yes — deep (`PARAVIRT_XXL`) |`

The write-protection analysis lives in **`06 §2.4` "Guest page-table writes"** (quoted in (D) below) — that is your real source for (i).

### (ii) PV hypercalls for PTE updates

`07 §3` (Historical PV designs table), **Xen PV row** (line 163):

> "2003 | Everything: ring-1 guest kernel, **read-only guest page tables + `HYPERVISOR_mmu_update` (batched (ptr,val) lists)**, event channels for interrupts, PV block/net, `HYPERVISOR_update_va_mapping`, hypercall-based descriptor/MSR/CR access | **<3–4 % vs native** (PostgreSQL/OSDB 4 %, SPEC WEB99 2 %) | **Partly.** The batched-PTE-update idea is the transferable core: it converts an unpredictable stream of traps into an explicit, batchable, semantically-rich call. But you only benefit if you maintain shadow/host page tables. **See the caveat below.**"

### (iii) Batching

Two places. The table row above ("**batched (ptr,val) lists**" … "it converts an unpredictable stream of traps into an explicit, batchable, semantically-rich call"), and the `PV TLB flush` row of §2.3 (line 145):

> `| **PV TLB flush** | Remote shootdown IPIs to descheduled vCPUs | Measured **~35 %** on a TLB-stress microbenchmark (Hyper-V, 3 m 33 s → 2 m 20 s) | **Moderate on SMP guests.** Also: the hook already exists in stock Linux (`pv_ops.mmu.flush_tlb_multi`) so this is *cheap PV* — not a kernel fork | Yes, but hook is upstream |`

Also §2.3 `PV IPI` row: "**Small.** Batch in the translator instead."

### (iv) Why classical PV is near-worthless to a DBT

**§0, conclusion 1** (lines 38–44) — the headline:

> "**The premise is mostly wrong, but for an interesting reason.** Almost every classical PV feature (PV-MMU, PV-EOI, PV-IPI, PV syscall, PV spinlocks) exists to **avoid VM exits** or to **avoid trapping non-virtualizable instructions**. A dynamic binary translator has *neither problem*: it never traps, because it rewrites; and a call from translated code into a C device model costs tens of nanoseconds, not the ~1–2 µs of a hardware VM exit. **PV features whose value is "one fewer exit" lose one to two orders of magnitude of value in a DBT.** The features that survive are the ones that avoid *work*, not exits."

**§3, "The Xen-PV-MMU caveat, stated precisely"** (lines 177–185) — this is the single most important passage for doc 20:

> "Xen PV-MMU removes the cost of **keeping a second set of page tables in sync**. It does *nothing* about the cost of **translating an address on every guest memory access** — because on Xen the hardware does that. In a DBT, the second cost is the dominant one and the first is a rounding error. **PV-MMU therefore optimizes the wrong half of the problem for a DBT that uses a software MMU, and is redundant for a DBT that uses the host MMU.** It becomes interesting only in the narrow band where you use host page tables *and* PTE-write interception is measurably hot (fork/exec/mmap-heavy workloads) — and even there, Linux's `set_pte` hooks are `PARAVIRT_XXL`-only, i.e. a guest fork."

**§3 Xen-on-ARM row** (line 166) — the "strongest single data point":

> "2012 | Devices, interrupts, timers only. Explicitly: **"no PV MMU calls, no shadow pagetables"** — uses stage-2 translation | (design simplification, not a benchmark) | **Yes — strongest single data point.** Given a clean second-stage MMU, the Xen team, who *invented* PV-MMU, chose not to build one again."

**§3 KVM row** (line 167): "Note KVM never added a PV-MMU — EPT/NPT made it pointless."

**§3 VMware VMI row** (line 169):

> "**Removed in Linux 2.6.37** because Intel VT-x/AMD-V hardware outperformed it. **The definitive precedent: a general-purpose PV-MMU interface was built, shipped, and deleted.**"

**§3 `paravirt_ops` today row** (line 170) — the ABI-level cost:

> "**Verified in current `arch/x86/include/asm/paravirt_types.h`:** what remains **unconditional** under plain `CONFIG_PARAVIRT` is small and useful — `pv_ops.mmu.flush_tlb_{user,kernel,one_user,multi}`, `pv_ops.mmu.exit_mmap`, `notify_page_enc_status_changed`, `pv_ops.irq.{safe_halt,halt}`, plus `pv_ops.lock.*` (pvqspinlock) and the PV sched-clock hook. Everything interesting for a PV-MMU — CR2/CR3, `set_pte`/`set_pmd`/alloc/release, descriptor tables, MSRs, `load_sp0`, `iret` — is **`PARAVIRT_XXL`, i.e. Xen-PV-only**. **Practical consequence: `flush_tlb_multi` is a free, upstream, no-fork PV hook you can use today. A real PV-MMU means turning on `PARAVIRT_XXL` and writing a Xen-PV-class backend — a guest fork.**"

**§7 recommendation 2** (lines 502–505):

> "**Copy KVM's shape, not Xen's.** … Note that KVM — with full hardware assist — **never shipped a PV-MMU**, and Xen-on-ARM explicitly declined to build one. **Do not build one.**"

### Cost of PV: guest modification / unmodified-guest requirement, with numbers

**§1** (lines 99–104) — the taxonomy that decides the cost:

> "- **Axes 1 and 2 (devices, interrupts/timers) require only a *driver*, not a modified kernel.** … This is what people usually mean when they say "PV" today and it is **not** true para-virtualization.
> - **Axes 4 and 5 (privileged instructions, page tables) require a modified kernel** — a different build, a different entry path, a hypercall ABI. This is true PV, and this is what forecloses Windows/macOS."

**§2.2** (lines 123–128):

> "**(a) and (b) are what "para-virtualization" literally means. They are also the two options with the smallest addressable market.** A patched-kernel-only emulator is a research artifact or an internal CI tool. **Xen PV — the most successful true-PV system ever built — has been in slow retreat since 2013 precisely because the ecosystem cost exceeded the performance benefit once hardware caught up.**
> **(c) is where essentially all modern value is.** Note carefully: **this is not para-virtualization.** It is "full emulation of the CPU + PV devices". Calling it PV is the most common error in this space."

**§5.3 — "the dispositive comparison"** (lines 385–388), the numbers you asked for:

| System | Measured |
|---|---|
| Xen PV vs native (2003, 32-bit) | **<3–4 %** overhead (PostgreSQL/OSDB 4 %, SPEC WEB99 2 %) |
| Xen PVHVM vs Xen PV | PVHVM **15–41 % faster** |
| Xen 64-bit PV vs HVM | PV can be **slower**, because syscalls require page-table switches / TLB flushes |
| Hyper-V PV TLB flush | **~35 %** on a TLB-stress microbenchmark (3 m 33 s → 2 m 20 s) |
| L4Linux vs native | **5–10 %** (AIM); up to **2.5×** on microbenchmarks |
| UML vs native | ~**20 %** typical, ~**50 %** on syscall-heavy |
| Rosetta-for-Linux vs QEMU | **~2–4×** (7-Zip), **4–5×** typical Docker, **>20×** in some reports; **~70–80 % of native** |

> "**The dispositive comparison:** the best true-PV system ever built bought **3 %** over native in its era, and **negative** performance a hardware generation later. Replacing the guest kernel with a native-ISA one buys **4–20×**. **PV as a fine-grained optimization is worth single-digit percent. PV as a whole-subsystem elimination is worth an order of magnitude. There is almost nothing in between.**"

Also `07 §3` Xen 64-bit row (line 164), directly relevant to your TTBR1 story:

> "**64-bit PV can be *slower* than HVM.** AMD removed segment limits in long mode, so Xen must put the guest kernel in ring 3 alongside userspace and use **separate page tables per privilege level; every syscall costs TLB flushes**, and there is no `sysret` hypercall (PV guests use `iret`) … **This is the cautionary tale.** True PV bought Xen ~3 % in 2003 and *negative* performance in 2008. Hardware ate the benefit."

**§5.1 hardware-vs-software MMU table** (line 334) — the shadow-paging cost datum:

> VMware, *Performance Evaluation of Intel EPT Hardware Assist*: "EPT vs shadow paging, ESX, same-ISA | **Up to 48 % on MMU-intensive applications; up to 600 % on MMU-intensive microbenchmarks**"

And `07 §8` open question #5 flags that number as second-hand: *"the per-benchmark table could not be extracted from the PDF and is quoted second-hand."*

**§7 recommendation 6** (line 526): *"**Do not build PV-SMC notification, PV-EOI, PV-IPI, PV context-switch notification, or PV syscall.**"* — note PV-MMU is not on this list; it's handled by recommendation 2.

---

# (B) `09-architectural-nuances.md` — nuances

Doc 09 is 247 lines, twelve nuances N-01…N-12. **No nuance mentions T1SZ or the canonical/TTBR split** — that claim is not in 09. Here are the ones that touch your topic, in full.

### Confidence legend (lines 6–7) — you should reuse this
> "Confidence: `V` = verified on this machine or in a spec, `R` = reported by a credible source, `I` = my inference, `?` = unverified."

Entry format (lines 3–4): *"Each entry: **what**, **why it bites**, **confidence**, **status** (Open / Resolved / Mitigated)."*

### N-11 — Only 256 ASIDs, against x86's 4096 PCIDs (full text, lines 215–229)

> **What.** `ID_AA64MMFR0_EL1.ASIDBits = 0` as seen from guest EL1 → **8-bit ASIDs (256)** (`V`, `probes/g1_mmu4k ids`). x86 `PCID` is 12-bit (4096). `VMIDBits = 0` likewise → 8-bit VMID.
>
> **Why it bites.** A shadow-paging design that allocates one ARM ASID per guest `CR3` runs out 16× sooner than the guest's own model expects. ASID exhaustion means recycling, and recycling means a broadcast TLB invalidate — precisely the storm that using the hardware MMU was supposed to avoid. A Linux guest with a few hundred live processes will cycle continuously.
>
> **Mitigation sketch.** Treat ASIDs as a cache over `CR3` values with an LRU/generation scheme rather than a bijection, the way Linux's own arm64 ASID allocator does; or pin ASIDs to the hottest address spaces and fall back to a flushed shared ASID for the cold tail (`I`).
>
> **Status.** Open — new, surfaced by the audit. Not costed anywhere yet.

**N-11 is now refuted quantitatively** by `16 §5`: *"**N-11 is quantitatively a non-issue.** 256 ASIDs versus 4096 PCIDs costs well under 1 % of a core even under pessimistic assumptions… `09` N-11's 'precisely the storm that using the hardware MMU was supposed to avoid' is **overstated**."*

### N-12 — No hardware Access/Dirty bit management (full text, lines 233–247)

> **What.** `ID_AA64MMFR1_EL1.HAFDBS = 0` (`V`) — Apple implements neither hardware Access-flag update nor hardware Dirty-bit management, on any generation checked.
>
> **Why it bites.** Every x86 `A` and `D` bit update must be produced by taking a fault in the shadow tables and emulating it. This is the last remaining hardware-assist gap in the MMU path now that 4 KB granule and 4 KB permissions are confirmed working, and it is the main reason the shadow design cannot simply mirror x86 PTEs and walk away.
>
> **Consolation.** `FEAT_PAN2` is present (`PAN = 0x2`), so `AT S1E1RP`/`AT S1E1WP` are available for fault-free permission probing, and `FEAT_XNX` (`XNX = 0x1`) gives distinct EL0/EL1 execute-never, which maps cleanly onto x86 `SMEP`.
>
> **Status.** Open — `12` ranks A/D last; that ranking predates this being the *only* remaining gap.

### N-02 — Page granule mismatch (lines 54–67) — touches write-protection granularity

> **Why it bites.** If we want the ARM hardware MMU to *be* the guest's MMU (shadow paging), the protection granularity must be ≤ the guest's. With only a 16 KB granule, one ARM PTE covers four x86 PTEs that may legitimately have different R/W/X/user permissions — so we would have to demote to software emulation whenever a 16 KB span is non-uniform. **Same problem for write-protecting code pages for self-modifying-code detection: 16 KB granularity means ~4x the false-positive write traps.**

Status (line 67) is now **RESOLVED — PASS**, with two corrections folded in from `14` and `16`: quads void; TLB reach loss "**~2×, not 4×**, costing 1.0–2.7× on TLB-bound code, worst in the 16–24 MiB band."

### N-01 — Host userspace VA is 47 bits, x86-64 guests want 48 (lines 24–51) — the canonical-boundary nuance

> **What.** `machdep.virtual_address_size = 47` on this Mac (`V`)… x86-64 defines a 48-bit canonical VA space (two 47-bit halves: `0x0000_0000_0000_0000..0x0000_7FFF_FFFF_FFFF` and `0xFFFF_8000_0000_0000..`), and every real x86-64 kernel lives in the upper half.
>
> **Why it bites.** The single fastest softmmu trick — map the entire guest VA space 1:1 into the host VA space and let the host MMU do the translation with zero software cost — needs the host to have *more* VA bits than the guest… **A flat identity map of a full x86-64 address space into a macOS process is not possible.**
>
> **Status. RESOLVED 2026-08-08 — helper VM gets 48 bits.** … Ceiling is also 48: `ID_AA64MMFR2_EL1.VARange=0` (no `FEAT_LVA`), therefore **x86 LA57 5-level paging cannot be shadowed and must not be advertised**.

### N-05 — ARM I-cache not coherent with D-cache (lines 106–123) — the coherence-adjacent nuance

> **Why it bites.** This is *not* the same problem as code-cache invalidation, but it compounds it. **Every guest write that lands on a page containing translated code must invalidate our translated blocks; guest kernels do this constantly** (module loading, `text_poke`/alternatives, ftrace, eBPF JIT, static keys). At 16 KB write-protect granularity (N-02) the trap rate could be brutal.
>
> **Mitigation sketch.** This is the single best paravirtualization opportunity: a modified guest kernel already calls `flush_icache_range()` on non-x86 architectures, so an x86 guest kernel patched to call a **hypercall at every code-patching site** gives us exact, cheap invalidation with no write-protection at all (`I`).
>
> **Status.** Open — flagged as a top-tier PV win.

⚠️ **N-05's mitigation directly contradicts `07 §3.2` (grade C)**, which says userspace JITs are unreachable and this is worth "maybe low single-digit percent". Doc 20 should note the contradiction.

### N-09 — Precise exceptions (lines 180–195) — relevant to fault-driven coherence

> "In full-system emulation, **page faults are not an error path, they are the mechanism.** The cost of maintaining restartability is paid on the hot path."

(Substantially superseded by `17 §0.4` finding 1 — shadow-miss faults are *not* guest-visible and need no precise state.)

---

# (C) `12-performance-ranking.md`

**The TTBR/canonical claim does not appear in 12** as a claim — only as a *mitigation token*. Rank 1 mitigation cell (line 31) ends: `` + `TTBR1` kernel-half `06:182` ``. That's the only TTBR reference in 12.

### Rank 8 — A/D + TLB reach (line 38, verbatim)

> `| **8** | **A/D + TLB reach** `06:204/225` | **`2` faults/page/epoch `0.15s/GiB`** + **`12→48 MiB` 4× loss** (3072+256 entries) `06:225` | `500–2000cyc/miss` model `06:182`, `1 GiB sweep 0.26–1.05 Gcyc` | `A` eager (legal `06:112`) → `1` fault/page, `2M` blocks `128 quads` `06:182`, `FEAT_TTL` `06:227` |`

**Rank 8 has been split by `16 §4`:** the 12 MiB half is *"confirmed exactly"*; the 48 MiB half is *"**wrong**: measured 16 KB reach is 16–32 MiB, so the loss from choosing 4 KB is **~2×, not 4×**"*. And `12`'s own correction header (lines 9–11) already says: *"**Rank 8 (A/D + TLB reach) is under-ranked.** `HAFDBS = 0` is now the only remaining hardware-assist gap in the MMU path. Also newly known: only **256 ASIDs** vs x86's 4096 PCIDs (`09` N-11), which is not costed anywhere in this table."*

### Rank 1 — softMMU cost and the 0.466 / 0.267 ns figures (line 31, verbatim)

> `| **1** | **Guest `ld/st` → host MMU** `05:390` `06:169` | **1.5× geomean (HSPT 1.51×, Captive 2.21× INT, 32× TLB)**. 9–11 insn/access (3 loads) vs 0 on shadow `06:172` | `V` softMMU 9-insn **0.466 ns vs `ldr` 0.267 ns = 1.74×** (`probe_mmu_tso.c`). Captive `67` vs `40` bytes `03:148` but `3.44×` faster blocks | Shadow paging `TCR_EL1.TG0/TG1=4K` `V` (`probe_g1_tgran_tso.c` `TGran4=0x0`) + `hv_vm_map` 4K `V` + `TTBR1` kernel-half `06:182` |`

**Its own header correction (lines 16–18):**

> "**Rank 1's `1.74x` softMMU microbenchmark is a lower bound on softmmu cost, not an estimate of it** — a hot fully-predicted loop omits I-cache bloat, slow-path mispredicts and register pressure. Its agreement with HSPT's 1.51x is coincidence, not corroboration."

`16 §"Implications"` para 2 reuses it correctly: *"`12` rank 1 costs a softMMU guest load at 0.466 ns versus 0.267 ns for a native `ldr`. Shadow paging makes the hot path a plain `ldr` and pays 146 ns per *miss*. **The break-even is one shadow fill per ~730 guest memory accesses**; any working set with better locality than that wins, which is essentially all of them."*

### Shadow-paging cost in 12

There is **no shadow-paging cost line item** in 12's rank table — shadow paging appears only as *mitigation*. The nearest cost model is Rank 4 (precise exceptions) and Rank 6 (quads, now void). This is a real gap doc 20 can fill.

### Page-table coherence / INVLPG in 12

**Nothing.** There is no ranking item for page-table coherence, guest PTE writes, INVLPG, or shadow invalidation anywhere in `12`. `INVLPG` does not appear in the file. **This is the strongest argument for doc 20's existence** — the concern is unranked.

Rank 5 (SMC/code-cache) is the closest neighbour: *"**`5–20%` if wrong**, `4×` false traps at 16K | `phys`-indexed cache `03:100` reuses across `fork`/`so`; `mprotect` vs `checkCRC` `05:507` | `phys` cache + rmap + checksum escalation"*.

---

# (D) `06-x86-system-semantics.md` — x86 TLB semantics, INVLPG, CR3, PCID, A/D, coherence

### §2.1 — "TLB coherence" (line 120) — **the load-bearing rule**

> "**TLB coherence** (SDM Vol.3A §4.10). The contract is: software must invalidate after changing a paging-structure entry, **except** that a not-present→present transition needs no invalidation, because the processor is forbidden from caching a translation for a non-present page. **This exception is what makes lazy shadow-table filling legal** — it is the load-bearing rule for the entire design. The processor may also cache intermediate *paging-structure* entries (PDE caches, §4.10.3), so the guest must invalidate on any level change; we get to inherit that obligation."

### §2.1 — INVLPG / INVPCID / MOV CR3 (line 122)

> "**`INVLPG`** invalidates entries for one linear address in the current PCID plus all global entries. **`INVPCID`** (SDM Vol.2) types: 0 = individual address, 1 = single context, 2 = all contexts *including* globals, 3 = all contexts *excluding* globals. **`MOV CR3`** flushes non-global entries for the current PCID (and if `CR4.PCIDE=1` and bit 63 of the new value is set, does *not* flush)."

### §2.1 — A/D bits (lines 110–114)

> "**A/D bits** (SDM Vol.3A §4.8). The processor sets `A` in every paging-structure entry used in a successful translation, and `D` in the final entry on a write. Key rules that emulators get wrong:
> - Updates are **atomic read-modify-writes** on the paging structure in memory, and are ordered as locked operations.
> - `A` **may be set speculatively** — for a translation the processor caches but never actually uses. **So being *over-eager* on `A` is architecturally legal.**
> - `D` is set **only on an actual write**. Being over-eager on `D` is *not* architecturally sanctioned; the practical damage is a spurious writeback of a clean page (performance, not correctness) — but a paranoid guest could notice.
> - If the paging structure is read-only in a nested/EPT layer, the A/D update itself faults. In our design the shadow tables are ours, so this reduces to "the update must be done by our fault handler.""

### §2.1 — self-referencing page tables / the aliasing premise (line 128)

> "**Self-referencing page tables.** Windows maps its PML4 into itself at a (since Win10 1607, randomized) index, so every page table is reachable at a computed virtual address. Linux uses a fixmap and `__va()` of the direct map. Either way, **guest page-table pages are ordinarily mapped writable in the guest's own address space, often at multiple virtual addresses.** This is the aliasing problem that forces a reverse map (§2.4)."

### §2.2 — the x86↔ARM equivalence table rows you need (lines 142–147)

> `| PCID (12-bit, 4096) | ASID (16-bit, 65536 — `FEAT_ASID16`) | Superset ✅ |`
> `| Global pages (`PTE.G`, `CR4.PGE`) | `nG` bit clear / `TTBR1` half | Exact |`
> `| `INVLPG` | `TLBI VAE1IS` | Exact |`
> `| `INVPCID` type 0/1/2/3 | `TLBI VAE1IS` / `ASIDE1IS` / `VMALLE1IS` / `VMALLE1IS`+global handling | Close |`
> `| `INVLPG` over a range | `TLBI RVAE1IS` (`FEAT_TLBIRANGE` ✅) | Better than x86 |`
> `| Hardware A/D | `FEAT_HAFDBS` — **absent on Apple** | ❌ Fault-driven (§2.5) |`

⚠️ The PCID row says **"ASID (16-bit, 65536 — `FEAT_ASID16`) | Superset ✅"** — this is **factually wrong** and was refuted by `13 §A-1` / `14`: measured `ASIDBits = 0` → **256 ASIDs**. §2.4's "With `FEAT_ASID16` (65 536 ASIDs) we never need to flush on a context switch" inherits the same error. **`06` has not been corrected.** Doc 20 must not repeat it.

### §2.4 — "**Guest page-table writes**" (lines 190–194) — the coherence protocol itself, verbatim

> "**Guest page-table writes.** Two options:
> - *Write-protect the guest PT pages* (KVM-style). Every guest PTE write faults. Guest OSes write PTEs constantly (`fork`, COW, `mmap`, demand paging) — this is expensive, and it interacts badly with 2 MiB-mapped PT arenas.
> - *Leave them writable and resync lazily* (virtual-TLB style). Rely on the guest's architectural obligation to `INVLPG`/`MOV CR3`. **On `INVLPG`, drop the shadow PTE + `TLBI VAE1IS`. On `MOV CR3`, either switch ASID (if the root is cached and clean) or rebuild. This is the right default**: it exploits exactly the same contract real hardware relies on, and it eliminates the PT-write fault storm.
>   - The one thing lazy resync cannot handle is a present→present *permission upgrade* without invalidation — which the architecture forbids the guest from doing, so it is safe. Present→present *downgrade* without invalidation is also forbidden.
>   - Hybrid: **write-protect only the *top* level (PML4), which changes rarely**, to catch address-space-wide restructuring cheaply."

### §2.4 — miss-avoidance levers, incl. the TTBR1 caveat (lines 185–188)

> "- **ASID per guest CR3/PCID.** … Guest `MOV CR3` becomes ~1 system-register write plus a hash lookup, instead of a full shadow rebuild. Recycle ASIDs with a generation counter (Linux's `arm64` ASID allocator is the reference design).
> - **Kernel half in `TTBR1`.** The guest's high canonical half (≥ `0xFFFF_8000_0000_0000`) is nearly always process-invariant. Shadow it once into `TTBR1_EL1` with `nG=0` and never touch it on CR3 switches. This gives x86 `PTE.G`/`CR4.PGE` semantics for free and makes CR3 switches genuinely cheap. **Caveat: the guest's high-half PML4 entries live in the *same* PML4 page as the low half, so a write there must invalidate the `TTBR1` shadow too — a special case in the write-protect handler.**
> - **Shadow root cache.** Keep N recent (CR3 → shadow root) pairs, LRU…
> - **Level sharing.** Because the trees are isomorphic, sub-trees for shared regions … can be shared between shadow roots … Requires refcounting."

### §2.4 — the fill sequence and cost (lines 176–182)

> "**Fill (lazy, on fault).** Legal because x86 forbids caching non-present translations (§2.1). On an ARM translation fault: 1. Take the abort at EL1 (`ESR_EL1.EC` = 0x20/0x21 instruction abort, 0x24/0x25 data abort; `FAR_EL1` = faulting VA; `ESR_EL1.WnR` bit 6 = write). 2. **Software-walk the guest x86 page tables** … This is unavoidable — it's how we distinguish "shadow miss" from "genuine guest `#PF`". 3. …populate the shadow L0–L3 chain, set A (and D if write), retry. 4. Else → synthesize an x86 `#PF`…
>
> **Cost per shadow miss:** … ≈ **500–2000 cycles**. Hardware x86 with EPT does a nested walk in tens of cycles. **This gap is the fundamental tax of design (B), and the entire optimization strategy is *miss avoidance*.**"

(`16 §2` measures this at **145.8 ns** without TLBI / **439.2 ns** with — i.e. `06`'s 500–2000 cyc estimate was roughly right for the *with-TLBI* case only.)

### §2.4 — aliasing / rmap (line 196) and BBM (line 198) and self-map (line 200)

> "**Aliasing and the reverse map.** … When we must revoke write access to a frame (for SMC detection, §7, or for D-bit tracking, or for a write-protected PT page), we must clear the write bit in **every shadow PTE that maps it**. That requires a `GPA → {shadow PTE*}` reverse map. KVM calls this `rmap`… **Budget for it explicitly; it is the single most-underestimated piece of shadow paging.**"

> "**Break-before-make.** `FEAT_BBM` = **no** on Apple. ARM ARM §D8.16 requires: to change a valid descriptor's block size, output address, or memory type, software must (1) write an invalid descriptor, (2) `DSB`, (3) `TLBI`, (4) `DSB`, (5) write the new descriptor, (6) `DSB`, `ISB`. That's a full round trip per change… **Mitigation:** … otherwise shatter to 4 KiB and accept the TLB-reach loss. Shattering avoids BBM entirely because you never change a block's size."

> "**Recursive/self-map interaction.** With a lazy-resync design, Windows' self-map is harmless: the guest reads and writes its own tables through ordinary mappings, and we resync on its `INVLPG`. **With a write-protect design it is a disaster: every self-map write faults. Another argument for lazy resync.**"

### §2.5 — A/D without HAFDBS (lines 202–216)

> "| `A` bit | Shadow PTE built with `AF=0` → first access raises an **Access Flag fault** … Handler sets guest `A=1` and shadow `AF=1`, retries. | 1 fault per page per shadow epoch |
> | `D` bit | Shadow PTE built with `AP[2]=1` (read-only) even when the guest permits writes → first write raises a **permission fault**. Handler sets guest `D=1`, flips shadow to writable, retries. | 1 additional fault per page per epoch |
>
> So: **up to 2 extra faults per page per shadow generation**, ~500–2000 cycles each, versus zero on real x86 hardware. For a workload touching 1 GiB of 4 KiB pages that is 262 144 pages × ~2 faults × ~1000 cyc ≈ **0.5 Gcycles ≈ 0.15 s** of pure A/D overhead per full sweep."
>
> Optimizations: "**Skip the A-bit fault when the guest PTE already has `A=1`.** … **Batch by block.** … Exploit the legality of speculative `A` setting (§2.1)… **Do not** do the same for `D`."

### §2.6 — whole-shadow invalidators (lines 218–222)

> "`CR0.WP=0` … has no ARM analogue. Handling: on a guest write to `CR0` that toggles `WP`, **flush all shadow roots** and rebuild… Cost per toggle is a full shadow rebuild — acceptable at low frequency, pathological if a guest toggles it in a loop… Similar whole-shadow invalidators: `CR4.PGE`, `CR4.SMEP`/`SMAP`, `CR4.PAE`, `CR4.PCIDE`, `CR4.LA57`, `EFER.NXE`, `EFER.LME`, and MTRR/PAT MSR writes. All rare; all just flush."

### §2.7 — TLB reach (lines 224–228)

> "Apple M1 Firestorm: L1 dTLB 256 entries, L2 TLB 3072 entries. At the native 16 KiB granule that is 48 MiB of reach; at 4 KiB it is **12 MiB**. … a **4× reduction in TLB reach** … This is a real, unavoidable, quantified tax — call it **5–15%** on memory-footprint-heavy guests."

(The 48 MiB figure is refuted by `16 §4`.)

### §6 — **NOT the AT proposal.** `06 §6` is "Flags" (EFLAGS/NZCV/AF/PF)

This is a **citation error propagated through the corpus**. `16 §6` says *"`06 §6`'s proposal to use `AT` for fault-free probing is **vindicated by measurement**"* and again *"`06 §6`'s proposal is confirmed"* — but `06 §6` is **§6. Flags** (lines 363–433). The actual AT proposal lives in **two other places**:

**`06 §5.3(a)` "Probe-then-execute" (line 346)** — the real source:

> "**(a) Probe-then-execute.** Before an instruction with multiple or wide memory operands, probe every operand for presence and permission (read and/or write) without committing anything. QEMU does this (`probe_write`, `access_prepare`). Under design (B) with hardware shadow tables there is a cheap probe: ARM's **`AT S1E1R`/`AT S1E1W`/`AT S1E0R`/`AT S1E0W` address-translation instructions** (and `FEAT_PAN2`'s `AT S1E1RP`/`S1E1WP`, which respect `PSTATE.PAN` — present on Apple). These ask the MMU "would this access succeed?" and return the result in `PAR_EL1` without side effects. **This is a genuinely useful hardware assist** — a probe costs one `AT` + one `MRS PAR_EL1` (**~10–20 cycles**) instead of a software walk."

**`06 §13` recommendation 9 (line 736):**

> "**Use `AT S1E0R`/`S1E1R`/`S1E1WP` for fault-free memory-operand probing** in multi-access instructions. Underused hardware assist."

`13 §A-1` cites it as *"the fault-free operand-probing trick `06 §4`"* — a **third** wrong section number. Doc 20 should cite `06 §5.3(a)` and `06 §13(9)` and note that "`06 §6`" in doc 16 is a mis-citation.

### §8 — SMC, for the write-protection/coherence overlap

`06 §8.3` "**Mechanism.** Write-protect, at **guest-physical** granularity, every frame containing translated code… **False sharing is the killer.**" and the escalation ladder (1. page-granular WP → 2. checksum on block entry → 3. interpret / generation counter → 4. sub-page dirty bitmap at 64/128 B).

`06 §8.4`: *"Also note `text_poke` writes through a **separate `mm`** with an alias mapping of the target page — so the write arrives at a *different guest virtual address* than the one we translated from. **Only guest-physical-granularity tracking catches this.** Virtual-address-based SMC detection is simply wrong for Linux."*

---

# (E) `13-audit-skeptical-review.md` — the evidence standard

### What the audit's method was (lines 4–6)

> "**Method:** re-decoded the raw probe output by hand against the ARM ARM instead of trusting the probe's own printout or the summarizing docs; **traced each headline number back to whether it was *measured*, *modelled*, or *cited*.**"

### The M/D/X/? tags — ⚠️ they are NOT defined in 13

`13` predates them. The `M`/`D`/`X`/`?` scheme was introduced by **`16`** and extended by **`17`**. `13`'s own scheme is the `V`/`R`/`I`/`?` scheme of `09` (see (B) above). The three legends in force:

| Doc | Legend, verbatim |
|---|---|
| `09` lines 6–7 | "`V` = verified on this machine or in a spec, `R` = reported by a credible source, `I` = my inference, `?` = unverified." |
| `16` lines 12–14 | "**Reading the labels.** `M` = measured on this machine today. `D` = derived by subtracting one measured quantity from another. `X` = extrapolated/modelled from an `M` number — arithmetic, not observation. `?` = attempted and **inconclusive**." |
| `17` lines 13–16 | "**Reading the labels.** `M` = measured on this machine today, **in-VM at guest EL1**. `D` = derived by subtracting one measured quantity from another. `X` = modelled/extrapolated — arithmetic on an `M` number, not an observation. **`C` = cited from a primary source** (SDM, ARM ARM, QEMU source, a paper). `?` = attempted and inconclusive." |

**For doc 20, use `17`'s five-tag legend** — it is the newest and the only one with `C`.

### What counts as measured — the rules 13 enforces

**Rule 1 — an ID-register read is not a working mechanism (`13 §B-1`):**

> "Look at what the probe actually executed (`run.log:31–46`): **15 instructions, all `mrs`/`msr`/`hvc`.** No page tables were built. `SCTLR_EL1.M` was never set. `TCR_EL1` was never written. **No address was ever translated by a 4 KB stage-1 regime.** The conclusion is an inference from an ID register to a working translation regime. That inference is *probably* right — but **"probably right" is exactly what a go/no-go gate is supposed to eliminate**…
> **What actually closes G-1:** a guest that builds a real 4 KB-granule L0–L3 table … and then demonstrates (a) a load through it returning the right data, (b) a *permission fault* at 4 KB granularity on the adjacent 4 KB page inside the same 16 KB host page. **Item (b) is the one that can genuinely fail.**"

**Rule 2 — a successful API call is not enforcement (`13 §B-1`):**

> "`run.log:87` shows a 4 KB `hv_vm_map` at a 4 KB-offset host VA *returned success*, but **no guest access was ever made through that mapping**. A successful `hv_vm_map` is not proof of correct 4 KB-granular translation or permission enforcement underneath a 16 KB host page."

**Rule 3 — a register read-back is not a behaviour (`13 §B-2`):**

> "it only proves the **bit is stored**, not that the **memory model changed**. A hypervisor that saves/restores the register without applying it passes this test… **Until one is, treat "hardware TSO works in the VM" as Medium confidence, not Verified**"

**Rule 4 — a self-calibrated model is not evidence (`13 §B-4`, "**`H = 55–87%` is a model output, not a measurement, and it is presented as data**"):**

> "1. **Circularity.** The parameters of `tools/g2_quad_measure.py --synthetic` were chosen by the same agent, calibrated to a qualitative table that the same agent wrote. **A model tuned to reproduce its author's priors is not independent evidence for those priors.**
> 2. **The metric dominates the result.** … **When a definitional choice swings the headline number by 4×, the number is not yet a measurement of anything.** …
> Until it is, **`H` should carry no weight in an architecture decision.**"

**Rule 5 — do not multiply non-independent speedups (`13 §B-5`):**

> "Captive's **2.21× over QEMU already includes Captive's own JIT**, its privilege mapping, *and* its host-MMU offload — it is an end-to-end number. Multiplying it … by a separately-sourced codegen factor double-counts. The multiplicands also come from different guests, hosts, baselines, and QEMU vintages… **The defensible claim today is:** *"the closest prior art got 2.2× over contemporaneous QEMU on SPECint; we have no independent evidence we would do better, and two identified reasons we might do worse."*"

**Rule 6 — a hot loop is a lower bound, not an estimate (`13 §B-6`):**

> "As evidence for the whole-program MMU win it omits every mechanism that makes softmmu actually expensive: ~9× instruction-cache footprint, the branch to the slow path and its mispredicts, the software-TLB miss path, and the register pressure… **A hot, perfectly-predicted, fully-pipelined loop is the best case for softmmu, so 1.74× is a *lower bound on the cost*, not an estimate of it.** That it lands near the independently-sourced 1.51× HSPT figure is **coincidence, not corroboration**"

`17 §0.4` finding 3 restates this as a standing rule: *"**Any future "the lookup is only N ns" claim must be measured on the fault path, not in a loop.**"*

**Rule 7 — ID registers read in a VM are the hypervisor's advertisement (`13 §A-2`):**

> "**every ID register the guest reads is HVF-mediated, not hardware.** Treat all G-1 ID-register results as "what Apple's hypervisor advertises", which is the right thing for *our* design, but do not generalise them to bare-metal Apple Silicon behaviour."

### MMU / shadow-paging-specific criticisms in 13

- **`§A-1`**: decoder bugs; the *newly surfaced* constraints `ASIDBits = 0` (256 ASIDs) — *"**This is a new, unlogged design constraint.** It is not in `06`, `09`, or `12`"* — and `HAFDBS = 0`, plus the good news `PAN = 2` → *"the fault-free operand-probing trick `06 §4` relies on is confirmed present, not assumed"* and `XNX = 1` → *"a clean way to shadow x86 `SMEP`"*.
- **`§B-3`**: G-2 obsolete; *"`11` and `12` need re-deriving against the G-1 result before `natemu-page-quads` gets any engineering time."*
- **`§C`**: *"**`07 §3.1`, `07 §6.4`, `11`, and `12` rank 6 form one dependency chain rooted in a refuted claim.**"*
- **`§E` (what still stands up)** explicitly endorses the isomorphism: *"**x86 4-level paging ↔ ARM 4 KB-granule stage-1 structural isomorphism** (`06`): this is an architectural fact, checkable on paper, and it is the genuine insight of the corpus."* — so doc 20 may lean on §0.1 but **not** on the ASID-16 claim.
- **`§D-4`**: *"**Single-machine, single-OS-version basis** … that flag should be promoted to a corpus-wide caveat."*
- **`§D-5`**: *"**No adversarial pass was run** … Independent verification, not summarization, is what was missing."*

Closing line (line 228): *"This audit deliberately does not soften its findings. The corpus is strong; the specific problem is that **summarization was mistaken for verification**, and two gates were marked closed on evidence that does not close them."*

---

# (F) `17-precise-exceptions.md` — the AT refutation, and reconciling it with 16's 26.4 ns

### `17 §0.2` measured-numbers rows 16–23 (verbatim)

| # | Quantity | Cost | Tag |
|---|---|---|---|
| 16 | `AT S1E1WP` **alone**, no `ISB`, no `PAR_EL1` read | **8.6 – 10.0 ns** | `M` |
| 17 | `AT S1E1WP` + `ISB` + `MRS PAR_EL1` (a usable probe) | **22.5 – 24.6 ns** | `M` |
| 18 | **Probing 1 operand before a store**, net of the store | **+24.5 ns = 24–30× a bare `STR`** | `D` |
| 19 | **Probing 2 operands before a store**, net | **+45.1 … +45.7 ns = 43–55× a bare `STR`** | `D` |
| 20 | `AT` on a **present-but-read-only** page (probe fails, permission) | **20.6 – 20.8 ns** | `M` |
| 21 | `AT` on an **unmapped** page (probe fails, translation) | **40.6 – 42.5 ns — 2.05×** | `M` |
| 22 | `AT S1E1RP`/`S1E1WP` report `PAR_EL1.F=1` + correct `FST` **taking zero exceptions** | **verified** | `M` |
| 23 | Bare `STR` control (same address stream) | 0.81 – 1.19 ns | `M` |

### `17 §0.4` finding 4 — the refutation, in full

> "4. **`AT`-based probe-then-execute is 25–55× a plain store and must not be used per instruction.** `06 §5.3(a)` and `06 §13(9)` promote `AT S1E1RP/WP` as *the* answer for multi-operand instructions. Measured: one probe is **+24.5 ns**, two are **+45.7 ns**, against a 0.9 ns store. `06`'s "~10–20 cycles" is wrong by 4–8× (`16 §6` already said so) and the *design consequence* — that probing is affordable per-instruction — is wrong outright. **Probing is affordable only when amortised over ≥ 100 elements (`REP` chunking) or gated behind a page-crossing test that is almost never taken.**"

### `17 §0.1` — the operative rule

> "…and `AT S1E1WP` used **only** where one probe amortises over hundreds of elements, never per-instruction."

### `17 §9.1` recommendation 7 (line 1222)

> "7. **`AT S1E1RP`/`S1E1WP` only where amortised over ≥ 100 elements or gated behind a ~0.2 %-taken [page-crossing test]**"

### `00-index.md:23` — the corpus-level summary of it

> "`AT` probe-then-execute **refuted** as a general answer (24-55x a store)."

### **How to reconcile 17's refutation with 16's 26.4 ns — they do not conflict**

They measure the **same instruction** and agree on its absolute cost; they differ only in what it is *divided by*.

- `16 §6` measures `AT S1E1R/W/RP/WP + ISB + MRS PAR_EL1` at **26.37 ns** (all four identical). `17` measures the same sequence at **22.5–24.6 ns**. Same number within run-to-run drift (`17`'s §8 documents DVFS drift; `19` reports ±20 % run-to-run).
- `16 §6` compares it against **a fault**: AF-fault-driven A-bit emulation costs 139 ns/page (no TLBI) or 441 ns/page (with TLBI). So `AT` is **5.3× cheaper than the cheapest fault-driven variant**, and 6.9 ms vs 36.4 ms per 1 GiB A-bit epoch (0.7 % vs 3.6 % of a core). *Verdict: use it.*
- `17` compares it against **a store**: 0.81–1.19 ns. So the same 24 ns is **24–55×**. *Verdict: never per-instruction.*

Both docs independently flag `06`'s "~10–20 cycles" as wrong: `16 §6` — *"`06`'s "10–20 cycles" for `AT` is **too optimistic by 4–8×**: it is ~86 cycles including the mandatory `ISB` and `PAR_EL1` read"*; `17 §0.4(4)` — *"wrong by 4–8× (`16 §6` already said so)"*.

**The clean statement for doc 20:** `AT` costs ~22–26 ns unconditionally. That is cheap per *page* (beats a 139–441 ns fault by 5.3×) and ruinous per *instruction* (24–55× a 0.9 ns store). For a page-table-coherence protocol, `AT` is on the *page* side of that line — an A-bit sweep, a batched permission re-probe after a guest PTE edit — so `16`'s verdict, not `17`'s, is the one that applies. Say so explicitly.

**Bonus lever both docs share** — `17 §5.4`, "The 'present-but-restricted rather than invalid' lever", confirmed twice independently:

> "**Guest page-table write-protection** and **SMC code-page protection** should keep entries **present-but-read-only, never invalid.** 3.3× cheaper on fault, 2.05× cheaper on probe."

and `16 §"Implications" 4`:

> "**Permission faults are 3.3× cheaper than translation faults** (28 vs 94 ns) because invalid descriptors can never be TLB-cached and must re-walk, nested through stage-2, every time. A shadow design that keeps entries *present but restricted* (for write-protection of guest page tables, SMC detection, dirty tracking) pays materially less than one that keeps them *invalid*. **This is a first-class design lever that the corpus has not previously identified.**"

That is arguably doc 20's central mechanism.

---

# (G) `11-pte-quad-gate.md` and `14-gate-results-verified.md`

### `11` — superseded, but two things survive for coherence

`11` opens with a **SUPERSEDED** banner (lines 3–14) — the quad gate is moot. What is still usable:

**`11 §1` bullet 2 (line 25)** — the exact coherence premise, cited to `06 §2.4`:

> "**06 §2.4** — shadow fill cost 500–2000 cyc/miss; miss-avoidance via ASID, TTBR1, root cache. **Whole design assumes lazy fill on `present→present` without guest invalidation is forbidden (so safe).**"

**`11 §3` metric definition** — the note on why A/D are excluded (lines 84–88):

> "Why loose on A/D: 06 §2.5 — Apple lacks FEAT_HAFDBS, so A faults (AF=0) and D faults (RO) are already extra faults per page per epoch. **Our handler sets guest A eagerly and promotes shadow RO→RW on D fault.** Counting A/D heterogeneity would double-count that cost and would force us to shatter quads that the handler already repairs in one fault."

**`11 §5` fallback 1 (line 159)** — shattering and its TLBI cost:

> "**Shatter + 4K shadows where heterogeneous** … If G-1 passes, shattering is correct and zero-per-access-overhead; cost is **4× TLB entries for those quads and 4× `TLBI` on invalidation** (manageable)."

**`11 §2` table, "Shared libs / `MAP_SHARED`" row (line 40)** — the rmap amplification:

> "They don't change `H` much, but they **amplify the *cost* of heterogeneity (same PFN appears in many shadows, needs rmap)**."

### `14` — the four gates, and what they mean for coherence

- **`§G-1a`** (4 KB stage-1 at guest EL1, **CLOSED, PASS**): `TCR_EL1 = 0x280903510` (`TG0 = 0b00` = 4 KB, `T0SZ = 16` = 48-bit VA, `IPS = 0b010` = 40-bit PA), writes `TTBR0_EL1` and `VBAR_EL1`, enables `SCTLR_EL1.{M,C,I}`. Six `[PASS]` lines. Falls out: *"**`T0SZ = 16` is accepted, i.e. a 48-bit guest VA space**"* and *"48 bits is also the ceiling … therefore **x86 5-level paging (LA57) cannot be shadowed and must not be advertised to the guest.**"* ← **only TTBR0 was exercised; TTBR1 untested.**
- **`§G-1b`** (4 KB permission enforcement inside one 16 KB host page, **CLOSED, PASS**) — *"This is the test that could actually have failed, and the one the entire shadow-paging design depends on."* `ESR_EL1 = 0x9600004f` → `EC = 0x25`, `ISS.DFSC = 0x0f` (permission fault, level 3), `ISS.WnR = 1`. **This is your evidence that 4 KB-granular write-protection of a guest PT page is enforceable.**
- **`§G-1c`** (4 KB stage-2 enforcement, **CLOSED, PASS**) — *"The previous evidence for this was that `hv_vm_map` *returned success*. It now demonstrably **enforces**."* → stage-2 write-protection at 4 KB is available for SMC/PT tracking.
- **`§G-4`** (hardware TSO, **CLOSED, PASS**) — 1,706,240/20,000,000 weak outcomes at `EnTSO=0`, **0** at `EnTSO=1`, with an 11× concurrency-witness control. Relevant to doc 20 because guest PTE stores are ordinary translated stores and their visibility ordering vs. the `INVLPG` that follows is a TSO question.
- **Residual costs** (line 93): *"4 KB pages cut TLB reach ~4× … and `HAFDBS = 0` means x86 A/D bits remain fault-driven."* (the ~4× is since corrected to ~2× by `16 §4`).
- **Corrected ID decode** (lines 145–179), the two new constraints: `ASIDBits = 0` → 256 ASIDs, `HAFDBS = 0` → fault-driven A/D. Plus `PAN = 0x2` → `AT S1E1RP/S1E1WP` available, `XNX = 0x1` → distinct EL0/EL1 XN.

---

# (H) Corpus-wide index of hits

Short index, doc:line → section, of everything you might be contradicting or should cite.

### "write-protect" / "write protection"
| Ref | Section | Content |
|---|---|---|
| `03:105` | §1.4 Captive | "physical pages from which code has been translated are flagged and **write-protected in the host page tables**" |
| `03:229` | §2.4 HyperMAMBO | "**the hypervisor write-protects the underlying guest physical page in the stage-2 tables**. A guest write traps to EL2 → DBT invalidates → protection removed → page dropped from the watch set." |
| `03:255`, `03:276`, `03:289`, `03:665` | §3 Transmeta CMS | sub-page/fine-grain write protection; removing it → "protection faults ×**46.8–59.4**"; Win95 boot "**52.8× more protection faults, 2.2× slowdown**" |
| `03:583`, `03:847` | §5.2 / §7 (3e) | self-referencing page tables: "**write-protection of guest PT pages must also cover the self-map window's virtual aliases**" |
| `03:882` | §7 blueprint item 7 | "SMC detection by **stage-2 write protection** (`hv_vm_protect`)" |
| `05:240`, `05:380` | Bochs / QEMU | `SMCChecks = mtrack`; QEMU `PAGE_WRITE_ORG` → `tb_invalidate_phys_page` |
| `06:97` | §1 table row 7 | "Write-protect at guest-physical granularity + rmap; escalate…" |
| `06:186` | §2.4 | the TTBR1 caveat — "a special case in the **write-protect handler**" |
| `06:191–194` | §2.4 | **the two options** (write-protect vs lazy resync) — your core citation |
| `06:196` | §2.4 | rmap |
| `06:200` | §2.4 | self-map: "With a write-protect design it is a disaster" |
| `06:515`, `06:525` | §8.3 | SMC write-protect mechanism + escalation ladder |
| `07:229`, `07:240`, `07:245` | §3.2 | PV-SMC grade C; "QEMU write-protects any page containing a translated block"; "PV 'relaxed SMC' mode" |
| `08:109` | §risks | "16K host pages inflate write-protect faults 4× on Tier 1" |
| `09:64`, `09:116`, `09:121` | N-02, N-05 | 16 KB WP granularity → 4× false traps; hypercall alternative |
| `16:446` | §Implications 4 | **"present but restricted" beats "invalid"** for PT write-protection |
| `17:951` | §5.4 | same, confirmed by a second mechanism |
| `17:731`, `17:763` | §3.4 | CMS's sub-page WP; "**sub-page write protection | yes** — 4 KB stage-1 + stage-2 (`14`)" |

### "INVLPG"
| Ref | Section | Content |
|---|---|---|
| `03:577` | §5.2 TLB invalidation | "x86 `INVLPG` is **core-local and non-broadcast**; SMP shootdown is done in software via IPIs. ARM `TLBI VAE1IS` is **hardware-broadcast**… This is an **opportunity**" |
| `03:814` | §7 (b2) | same point, as an Apple-Silicon advantage |
| `03:864` | §7 (8) | "Multi-vCPU adds: cross-vCPU shadow-table shootdown (guest `INVLPG`/`INVPCID`/`MOV CR3` must invalidate…)" |
| `05:276` | Bochs gaps | `INVLPG` listed among privileged ops |
| `06:47` | §0.2 | `FEAT_TLBIRANGE/TLBIOS/TTL` yes → "Range invalidation for `INVLPG` bursts and `INVPCID`" |
| `06:122` | §2.1 | exact `INVLPG`/`INVPCID`/`MOV CR3` semantics |
| `06:144–146` | §2.2 | `INVLPG`→`TLBI VAE1IS` **Exact**; range → `TLBI RVAE1IS` **Better than x86** |
| `06:192` | §2.4 | "On `INVLPG`, drop the shadow PTE + `TLBI VAE1IS`" |
| `06:200` | §2.4 | "we resync on its `INVLPG`" |
| `08:73` | §design | "lazy resync on `INVLPG`/`MOV CR3`, BBM handling" |
| `15:458` | §6 MTTCG | "`INVLPG`/IPI shootdowns) is where MTTCG's costs live" |
| **`12`** | — | **absent — no rank item** |

### "coherence"
`06:120` §2.1 (**TLB coherence**, SDM §4.10) · `06:501` §8.1 (I-cache not coherent) · `09` N-05 header · `03:672` §6.4 ("Full-system SMC coherence") · `07:234` §3.2 ("the I-cache is coherent with stores") · `02:776` (Apple requires I-cache maintenance) · `01:369`, `13:86` (host/guest register coherence — unrelated sense).

### "shadow"
Dense; the load-bearing ones: `06 §2.3(c)`, `§2.4` (whole section), `§2.5`, `§2.6`; `16 §2` (fill measured at 145.8/439.2 ns), `16 §"Design rule"`; `19 §1a/1b/1c` (scaling: 92 % efficiency at 8 vCPUs private, +9 % shared, **45 M fills/s aggregate**; with per-fill TLBI → 6.6 M/s, **6.8× worse**); `17 §0.1/0.4(1)` (shadow-miss ≠ guest-visible fault); `03:536` ("**hardware assist only wins if the exits are rare, and page-table maintenance is where exits concentrate**" … "Captive's design largely sidesteps it — because the *guest's* page-table stores are just ordinary translated stores to guest RAM, and the shadow is rebuilt lazily on fault, **there is no per-PTE-write trap at all**"); `03:589` (BBM: "A shadow that naively mirrors guest PTE edits will violate BBM and can produce a **TLB conflict abort**"); `03:591` (dual shadow address spaces for the AP-lattice/`CR0.WP` mismatch); `15:505`, `15:629`, `15:702`.

### "TTBR1" / "TTBR0"
`06:19`, `06:25`, `06:27` (§0.1) · `06:136` (§2.2) · `06:91` (§1) · `06:170` (§2.3c) · `06:186` (§2.4 kernel-half + caveat) · `06:265` (§4.1 nanokernel VA protection) · `03:792` (§7(4) O(1) invalidation) · `03:808`, `03:878` (§7(a)) · `07:322`, `07:423` (§3.7, Tier 2) · `08:20`, `08:47`, `08:73` · `11:81`, `11:92` (H_user/H_kernel split) · `12:31` · `16:34`, `16:277`, `16:285` (§5, ASID switch = **10.3 ns**) · `19:39`, `19:204` (all vCPUs share one TTBR0/ASID) · `02:180`, `02:269`, `02:830`, `10:130` (ISV=0 decoder needs a TTBR walker).

### "canonical" (paging sense)
`06:19`, `06:25`, `06:27`, `06:136`, `06:186` · `09:27`, `09:38` (N-01) · `03:808` · `08:47`. (Non-paging senses: `06:279`/`06:93` SYSRET non-canonical `#GP`; `03:536`/`03:622`, `05:529`, `05:772`, `05:806`, `04:164` — ignore.)

### "T1SZ" / "T0SZ"
`06:19`, `06:136` (`T0SZ=T1SZ=16`) · `08:47` (`T0SZ/T1SZ=16`) · `09:47` (`T0SZ=16` measured) · `13:78`, `13:216` (audit demanded T0SZ=16 be verified) · `14:17`, `14:33`, `14:190` (**closed: `T0SZ=16` works, 48 is the ceiling**). **`T1SZ` never measured.**

### "self-modifying page table"
**Zero hits** for that exact phrase. The concept lives as "self-referencing / recursive / self-map page tables": `06:128` (§2.1), `06:200` (§2.4), `03:583` (§5.2), `03:847` (§7(3e)).

### "PV" / "paravirt"
All of `07` (esp. §1, §2.3, §3, §7) · `09` N-05 (top-tier PV win) · `11 §5` (`natemu-page-quads`, superseded) · `12` rank 6 (void) · `08 §8` · `00:16` (index verdict) · `04:51`, `04:998` (Rosetta: "**There is no Apple-specific hypercall and no paravirtual TSO interface**").

### "hypercall"
`07:94`, `07:103`, `07:116`, `07:143`, `07:163`, `07:243`, `07:555` · `09:120` (N-05 SMC hypercall) · `03:208`, `03:228` (HyperMAMBO: guest TLB-flush instructions trapped to EL2; page-granular (VA,ASID) registration + hash-table filter) · `02:223` (`BRK`/`HVC` as host hypercall) · `16:91` ("not a disguised hypercall") · `19:777` · `04:51`, `04:998`.

---

# House style — skeleton for doc 20

The corpus has **two** styles. Docs 01–12 are the older, looser style. **Docs 14, 16, 17, 19 are the current house style** — they were written after the audit and are what doc 20 should match. Here is the pattern, extracted from `16`, `17` and `19` (which agree almost exactly):

```markdown
# 20 — <Question-shaped title, ending in "Measured" if it is>

**Date:** 2026-08-08 · **Host:** Apple M2 Max, macOS 26.5.2, SDK Hypervisor 259.5.15
**Probe:** `probes/<name>/<name>.c` + `probes/<name>/payload.h`
(build `probes/build.sh <name>/<name>.c`; raw log `probes/<name>/run-all.log`)

<One paragraph: what earlier doc established the mechanism, and what THIS doc adds.
16 does this in 4 lines: "`14` proved the *mechanism* exists… This document measures what the
mechanism **costs**." Include the measurement-hygiene sentence if applicable — e.g. 16's
"Every number below was taken with `CNTVCT_EL0` read **inside the VM at guest EL1**".>

> **Reading the labels.** `M` = measured on this machine today, in-VM at guest EL1.
> `D` = derived by subtracting one measured quantity from another. `X` = modelled/extrapolated —
> arithmetic on an `M` number, not an observation. `C` = cited from a primary source (SDM, ARM ARM,
> QEMU source, a paper). `?` = attempted and inconclusive.

---

## 0. Bottom line          <- 17 numbers it "## 0."; 16 and 19 use bare "## Bottom line"

### 0.1 The recommendation in one paragraph     <- 17's shape; strongly recommended

### 0.2 The measured numbers this rests on
| # | Quantity | Cost | Tag |          <- 16 adds a "Cyc @3.25 GHz" column
|---|---|---|---|
| 1 | … | **bold the headline value** | `M` |

**Reference points this must be compared against** (`02`, `12`, `14`, `16`): …

### 0.3 <The cost model / the one table that matters>
### 0.4 The <N> findings that change the design       <- numbered, each **bolded lead sentence**
### 0.5 The invariant / the design rule               <- blockquoted

### The single most important number                  <- 16's device; a blockquote, 3-5 lines
> **<claim>**
> …
> **It is not X. It is Y.** <- the corpus likes explicit self-correction of prior claims

---

## Method and its guards        <- MANDATORY. 16 and 19 both have it. Bullets:
* **Clock.** …
* **Counter-read overhead measured and accounted.** …
* **Every fault measurement has a matched control.** …
* **<X>-vs-<Y> guard.** …
* **Trap guard.** …
* **Hang safety.** …
* **Spread.** median of N rounds, 10th/90th percentile, repeated across K process invocations

---

## 1 — <Experiment>            <- numbered "## N — Title", em-dash, no period
```
raw probe output, indented, verbatim, including the `[PASS]` lines
```
**<N> things fall out, and <M> of them were not expected:**   <- 16's phrasing
1. **<bolded claim>.** …
*A methodological note worth recording:* …                     <- record failures

## 2 — …
### <Sub-finding with a verdict in the heading>   e.g. "The TLBI on the fill path is unnecessary,
                                                   and dropping it is a 3× win"

---

## Implications for the Tier 2 design
### Does <the thesis> survive its own costs? Yes — with one mandatory design rule.
**1. Versus <alternative>: …**
> **Design rule, and it is the main deliverable of this document:**
> **<rule in bold>**

**4. Secondary conclusions that change the corpus.**
* **`09` N-12 (A/D) should be re-scoped from … to …**
* **`12` rank 8 splits.** …
* **`06`'s "X" is wrong by 4–8×.** …

### The design rules that follow          <- 19's variant; numbered 1..7
### The honest summary                    <- 19; blockquote

---

## Corrections and additions to the corpus     <- 19 has this as its own H2; use it
* **`NN §S` "<quoted prior claim>" is superseded.** …
* **`NN §S` "<quoted prior claim>" is confirmed and strengthened.** …
* **New: …**

## What is still open
* **Everything here is one machine, one OS, one process.** M2 Max, macOS 26.5.2 …
* …

**Reproduce:**
`cd probes && ./build.sh <name>/<name>.c && ./<name>/bin/<name> all`
(individual experiments: `a`, `b`, `c`).

---

## Auditor's note (added on review, 2026-08-08)     <- optional; 19 has one
```

### Style rules the corpus follows consistently

1. **Backtick every identifier and every cross-reference.** Docs are cited as `` `16 §2` ``, `` `09` N-11 ``, `` `06 §2.4` ``, or line-anchored as `` `06:182` `` (12's style). Both are in use; §-refs are preferred in 14/16/17/19.
2. **Bold the load-bearing clause of every bullet**, usually the first sentence.
3. **Units:** `ns` with a space, `µs`, `×` (multiplication sign, not `x`) for ratios — except doc 12 and `00-index`, which use `x`. Use `×`.
4. **Every number carries a tag** (`M`/`D`/`X`/`C`/`?`) or an explicit "modelled".
5. **Report failures.** `16 §7` reports an INCONCLUSIVE result at length and says *"we are not going to report a number we cannot reproduce"*; `14 §G-4` records the negative lockstep result; `19`'s auditor's note contradicts `19` itself. This is expected.
6. **Explicitly supersede prior claims by quoting them.** Every recent doc has a section that quotes a prior doc's sentence and says whether it survives.
7. **Blockquote (`>`) for the one-or-two claims the document exists to make.** Never more than ~4 per doc.
8. **Retraction banners at the very top** when a doc's premise dies (`07`, `11`) — `> **RETRACTION NOTICE (date).**` / `> **SUPERSEDED (date).**`, followed by exactly what is void and what survives.
9. **Corpus-wide caveat** near the end: single machine, single OS version, re-run on M1/M3/M4 (`13 §D-4` demands it; `14`, `16`, `19` all carry it).

### Specific things doc 20 should do given all of the above

- Use `17`'s five-tag legend (`M`/`D`/`X`/`C`/`?`).
- Open by naming the gap: **page-table coherence is unranked in `12` and `INVLPG` appears nowhere in it.**
- Cite `06 §2.1` (TLB coherence contract) as `C`, `06 §2.4` (write-protect vs lazy-resync) as the design fork, `16 §"Design rule"` + `19 §"design rules" 1` as the measured constraint, `17 §5.4` / `16 §Implications 4` as the "present-but-restricted" lever, `14 §G-1b/G-1c` as the enforcement evidence.
- **Correct three things in passing:** (1) `T0SZ=T1SZ=**16**`, not 17, and `T1SZ` is untested; (2) `06 §2.2`'s "ASID (16-bit, 65536 — `FEAT_ASID16`) Superset ✅" is refuted (`ASIDBits=0`, 256); (3) doc 16's "`06 §6`" citation for the `AT` proposal should be `06 §5.3(a)` / `06 §13(9)`.
- Reconcile 16 vs 17 on `AT` explicitly: same ~24 ns, different denominator (page vs instruction); coherence work is on the page side.