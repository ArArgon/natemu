# 14 — Gate Results, Actually Verified

**Date:** 2026-08-08 · **Host:** Apple M2 Max, macOS 26.5.2 (25F84), SDK Hypervisor 259.5.15
**Probes:** `probes/g1_mmu4k/g1_mmu4k.c`, `probes/tso_litmus/tso_litmus.c` (build: `probes/build.sh`,
ad-hoc signed with `com.apple.security.hypervisor`, SIP untouched)

This document supersedes the G-1 status in `00-index.md` and `09` N-02. It exists because
`13-audit-skeptical-review.md` found that G-1 had been marked CLOSED on evidence that did not
close it: the original probe read ID registers and inferred a working translation regime, and
verified TSO by reading a bit back. Both gates have now been closed by experiment.

---

## G-1a — 4 KB stage-1 translation at guest EL1 · **CLOSED, PASS**

The guest builds a real 4-level, 4 KB-granule stage-1 table, sets
`TCR_EL1 = 0x280903510` (`TG0 = 0b00` = 4 KB, `T0SZ = 16` = 48-bit VA, `IPS = 0b010` = 40-bit PA),
writes `TTBR0_EL1` and `VBAR_EL1`, enables `SCTLR_EL1.{M,C,I}`, and continues executing.

```
  -- checkpoint 0: SCTLR_EL1=0x30101185 (M=1 C=1 I=1)
  [PASS] reached checkpoint 1 (MMU enabled, code still executing)
  [PASS] SCTLR_EL1.M == 1 -- stage-1 MMU actually ON
  [PASS] load through 4 KB stage-1 translation at 48-bit VA returned correct data
  [PASS] store through 4 KB stage-1 translation to RW page took effect
  [PASS] store landed at the right host address
  [PASS] read of the adjacent RO 4 KB page returned correct data
```

Two facts fall out of this that were previously only inferred:

- **4 KB granule stage-1 works at guest EL1**, not merely "is advertised in `ID_AA64MMFR0_EL1`".
- **`T0SZ = 16` is accepted, i.e. a 48-bit guest VA space.** The test address is
  `0x0000_4000_0000_0000`, which is out of range under the 39-bit `T0SZ = 25` configuration and
  would have produced an address-size fault. This closes the open half of `09` N-01: the helper-VM
  design does get the 48 VA bits that a macOS process (47) cannot provide. `ID_AA64MMFR2_EL1`
  reports `VARange = 0`, so 48 bits is also the ceiling — no `FEAT_LVA`, and therefore **x86 5-level
  paging (LA57) cannot be shadowed and must not be advertised to the guest.**

## G-1b — 4 KB permission enforcement inside one 16 KB host page · **CLOSED, PASS**

This is the test that could actually have failed, and the one the entire shadow-paging design
depends on. `DATA_A` (IPA `0x40010000`) and `DATA_B` (IPA `0x40011000`) are adjacent 4 KB pages
inside a **single 16 KB host page** (`0x40010000`–`0x40013fff`). They are mapped RW and RO
respectively in the same L3 table.

```
  -- guest took an exception: ESR_EL1=0x9600004f EC=0x25 (PERMISSION FAULT)
                              FAR_EL1=0x400000001000 ELR=0x4000004c
  [PASS] exception is a data abort from current EL (EC=0x25)
  [PASS] fault status is PERMISSION fault (not translation/access-flag)
  [PASS] fault was on a write (ESR.WnR=1)
  [PASS] FAR_EL1 == the RO page's VA
  [PASS] RO page contents unmodified
```

`ESR_EL1 = 0x9600004f` decodes as `EC = 0x25` (data abort, current EL), `ISS.DFSC = 0x0f`
(permission fault, level 3), `ISS.WnR = 1` (write). The write to the RO half was faulted while the
write to the RW half in the *same host page* succeeded.

## G-1c — 4 KB stage-2 enforcement inside one 16 KB host page · **CLOSED, PASS**

Same question one level down. With `HV_IPA_GRANULE_4KB`, one 16 KB host `mmap` is split into two
4 KB stage-2 mappings with different permissions.

```
  host page = 0x102858000 (16 KB), split into 2 x 4 KB stage-2 mappings
  [PASS] guest read the read-only 4 KB stage-2 page
  [PASS] read returned the right data from host page offset 4096
  -- stage-2 abort: ESR=0x93c1804f EC=0x24 IPA=0x40001000 VA=0x40001000
  [PASS] stage-2 fault status is PERMISSION fault
  [PASS] fault was on a write
  [PASS] faulting IPA is the RO 4 KB page
  [PASS] RO page contents unmodified
```

The previous evidence for this was that `hv_vm_map` *returned success*. It now demonstrably
**enforces**.

### Consequence: G-2 (`natemu-page-quads`) is no longer justified

`11-pte-quad-gate.md:11` states the premise for the quad PV feature explicitly: 1:1 shadow paging
is possible *"only at 4K stage-1 … but stage-2 `hv_vm_map` and host `mmap` default to 16K."* That
premise is now refuted at both levels — the 16 KB granule is the **default**, not the **only**
option, and 4 KB is enforced end to end.

With 4 KB stage-1 and 4 KB stage-2, each guest x86 4 KB PTE maps to exactly one shadow PTE. The
`n:m` degraded path that `natemu-page-quads` existed to avoid does not arise, so the PTE-quad
homogeneity metric `H` no longer gates anything. **`07 §3.1`/`§6.4`, `11`, and `12` rank 6 should be
retracted or rewritten**; `12`'s headline projection loses the "15–35% of the MMU win" penalty and
`07`'s "one A-grade PV idea" no longer has a problem to solve.

*Residual costs that are unchanged and still real:* 4 KB pages cut TLB reach ~4× versus the 16 KB
pages Apple's TLBs are sized for, and `HAFDBS = 0` means x86 A/D bits remain fault-driven.

---

## G-4 (new) — Hardware TSO genuinely changes the memory model · **CLOSED, PASS**

The prior evidence was a write-then-read-back of `ACTLR_EL1`. That is Asahi's screening test and it
rules out "the hypervisor ignores the write", but a hypervisor that context-switches the register
without applying it passes it too.

**Litmus.** Two vCPUs on two host threads, MMU on, Normal Inner-Shareable Write-Back memory (with
the MMU off, guest memory is Device-nGnRnE and the test would be vacuous). Free-running, no
synchronization in the measured window:

```
  P0:  X = i ; Y = i          (i monotonically increasing, no barrier between the stores)
  P1:  r0 = Y ; r1 = X        (no barrier between the loads)
```

P0 writes X before Y and both only increase, so under TSO any observed `Y` implies `X` has already
reached at least that value: `r1 >= r0` must hold. **`r0 > r1` is proof of reordering.**

> The store-buffering (SB) litmus would have been the wrong test — TSO *permits* SB, so it cannot
> distinguish TSO from ARM's weak model.

**Result**, 20,000,000 iterations per round:

| Round | `ACTLR_EL1` in guest | Weak-memory outcomes | Concurrency witness |
|---|---|---|---|
| `EnTSO = 0` | `0x0` (EnTSO=0) | **1,706,240 / 20,000,000** (8.5%) | 810,345 |
| `EnTSO = 1` | `0x2` (EnTSO=1) | **0 / 20,000,000** | 8,897,164 |

**Control.** The obvious alternative explanation — that enabling TSO merely destroyed thread
overlap, so P1 never caught P0 mid-flight — is ruled out by the concurrency witness (the number of
iterations in which P1 observed `Y` change under it). In the TSO round the threads overlapped
**11× more**, not less, and still produced zero violations.

An earlier lockstep-synchronized MP formulation produced 0 violations in *both* rounds and the
harness correctly reported INCONCLUSIVE rather than a false pass; the per-iteration handshake had
closed the race window. Recorded here because the negative result is what motivated the
free-running design.

**Verdict:** `ACTLR_EL1.EnTSO` set from guest EL1 under Hypervisor.framework on macOS 26.5.2
**genuinely enforces store→store and load→load ordering**. `01`'s TSO conclusion is upgraded from
read-back evidence to behavioural evidence.

*Not measured here:* the performance cost of running with `EnTSO = 1`. `01` cites 2–5%; that
number remains unverified on this host.

---

## Corrected ID register decode (fixes `13` A-1)

Read from guest EL1 by `g1_mmu4k ids`, decoded against ARM DDI 0487:

```
ID_AA64MMFR0_EL1 = 0x000010000f100002
  TGran4   [31:28] = 0x0   4 KB SUPPORTED
  TGran64  [27:24] = 0xf   not supported
  TGran16  [23:20] = 0x1   16 KB SUPPORTED     <-- old probe printed "NOT SUPPORTED"
  ASIDBits  [7:4]  = 0x0   8-bit ASID (256)    <-- never reported before
  PARange   [3:0]  = 0x2   40 bits / 1 TB      <-- old probe printed "32 bits"
ID_AA64MMFR1_EL1 = 0x0000000011212000
  HAFDBS    [3:0]  = 0x0   no hardware Access/Dirty bit management
  VMIDBits  [7:4]  = 0x0   8-bit VMID
  PAN     [23:20]  = 0x2   FEAT_PAN2 -- AT S1E1RP/S1E1WP available
  XNX     [31:28]  = 0x1   FEAT_XNX -- distinct EL0/EL1 execute-never
ID_AA64MMFR2_EL1 = 0x1001001100001011
  VARange [19:16]  = 0x0   48-bit VA max (no FEAT_LVA)
MIDR_EL1 = 0x610f0000     implementer=0x61 (Apple) part=0x0  <-- synthetic/masked by HVF
AIDR_EL1 = 0x6afd5797     bit9 (TSO capable) = 1
```

The raw values reproduce the original probe's exactly; only the decode differed. Two new design
constraints follow, neither previously logged:

- **`ASIDBits = 0` → 256 ASIDs**, against x86's 12-bit (4096) PCID space. A design assigning one ARM
  ASID per guest `CR3` will recycle — and therefore flush — far sooner than the guest expects.
  Needs an explicit ASID allocation/recycling policy.
- **`HAFDBS = 0` → no hardware A/D**, so every x86 Accessed/Dirty bit update is fault-driven.
  `12` ranks this last (rank 8); given it is now the *only* remaining hardware-assist gap in the
  MMU path, it deserves re-ranking.

**Also worth noting:** `MIDR_EL1` part is masked to 0, so every ID value above is *what Apple's
hypervisor advertises to a guest*, which is the right thing for this design but must not be
generalised to bare-metal Apple Silicon.

---

## Gate status after this pass

| Gate | Was | Now |
|---|---|---|
| G-1 4 KB stage-1 at guest EL1 | CLOSED (inferred from ID register) | **CLOSED — demonstrated** |
| G-1b 4 KB perms in one 16 KB host page | not tested | **CLOSED — PASS** |
| G-1c 4 KB stage-2 enforcement | "hv_vm_map returned success" | **CLOSED — PASS** |
| 48-bit guest VA (`09` N-01) | open | **CLOSED — `T0SZ=16` works; 48 is also the ceiling** |
| G-4 hardware TSO in VM | read-back only | **CLOSED — litmus, 1.7M → 0** |
| G-2 PTE-quad homogeneity | "mandatory" | **MOOT — premise refuted, see above** |

**Reproduce:** `cd probes && ./build.sh g1_mmu4k/g1_mmu4k.c && ./g1_mmu4k/bin/g1_mmu4k stage1`
(also `stage2`, `ids`); `./build.sh tso_litmus/tso_litmus.c && ./tso_litmus/bin/tso_litmus 20000000`.

**Caveat, corpus-wide:** single machine (M2 Max), single OS version (macOS 26.5.2). The 4 KB IPA
granule is macOS 26+ only, and `ACTLR_EL1` writability from guest EL1 is reported broken on older
macOS. Re-run on M1/M3/M4 and on an older macOS before treating any of this as a platform
guarantee.
