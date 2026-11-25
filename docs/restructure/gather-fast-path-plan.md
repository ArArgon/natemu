# AVX2 gathers inside generated code: plan

November 25, 2026, on `gather-fast-path` (from `main` at e38ae98c). Reviewed by Fable the same day: the core design holds;
its four blocking findings and the rest are folded in below (section 6 lists them).

## 1. Why

The eight AVX2 gathers (VEX.66.0F38 90-93 with a VSIB operand: VPGATHERDD/DQ/QD/QQ, VGATHERDPS/DPD/QPS/QPD) are a system
cut today ([fexc_slice1.inc](../../tools/fexc/shim/fexc_slice1.inc):326-328): every gather ends its block, the resident looks
it up, runs [`gather::transaction`](../../crates/runtime/src/execution/gather.rs) (each element through a full software walk of
the guest's page tables, [segments.rs](../../crates/runtime/src/execution/segments.rs):428-490) and enters the next block. An
exit into a system instruction is never linked. That path was built for exactness in the correctness pass
([correctness-userspace.md](correctness-userspace.md):188-240) and never made fast.

**An experiment** (M2 Max, natemu 723b9247; glmark2 2023.01 off-screen under Xvfb on Mesa 25.0.7 llvmpipe, 4 vCPUs; the
recipe in `.natemu/llvmpipe-exp/`, not a framework case): llvmpipe samples textures with AVX2 gathers. In the texture
scenes the resident's `system` bucket is 62% of vCPU time, about 70 million gathers in 30 s (`k1.systems.kinds.0x7a`),
about 1.1 µs each; translation is 5%. natemu scores 191 against QEMU TCG's 23, but is slower than QEMU on every
texture-heavy scene (texture 21 against 55 FPS, a blurred desktop 1 against 4). With llvmpipe held to SSE4.1
(`GALLIUM_OVERRIDE_CPU_CAPS=sse4.1`, no gathers) natemu scores 543 against QEMU's 24 and a native ARM64 VM's 728 on the
same Mac. A GNOME desktop on the Bochs display is llvmpipe, so this is most of why it feels slow.

Expected after this plan: the AVX2 score close to the SSE4.1 one (natemu runs 256-bit operations as two 128-bit halves, so
AVX2 gains little over SSE here, and gathers become a few plain loads).

## 2. What to build

A gather in a block compiled with `--bulk` (the host's flag for every translate request that is not a single-instruction
step and not alignment-gated: [execution.rs](../../crates/host/src/execution.rs):4793, so never with TF stepping, data
breakpoints or alignment checking armed: `alignment_gated` is `Cpu::bit_tests_cut()`, alignment checking or a data
breakpoint enabled in DR7 with R/W 01 or 11, code-context bit 12; an instruction breakpoint does not gate it) runs inside the block, in 64-bit code, with no 0x67 prefix and no FS or GS
segment. Every other gather (a step, 32-bit code, 0x67, FS/GS, `--avx-gated`) stays the system cut, unchanged.

### 2.1 The IR (fexc, the FEX fork)

In `AVX128_VPGATHER`, under `fexc_slice1 && fexc_bulk` and the conditions above, instead of `_VLoadVectorGatherMasked`:

1. Load dest, mask and the VSIB index as today (`AVX128_LoadSource_WithOpSize`, `AVX128_LoadVSIB`); base + displacement
   as FEX computes it.
2. For each data element `i` of the form (n = 4 or 8 for DD/PS, 2 or 4 for DQ/PD/QQ, 2 or 4 for QD/QPS): its index element
   to a GPR (`_VExtractToGPR`, sign-extended for 32-bit indices), `addr = base + (index << log2(scale))` with ordinary IR
   ops, then **`FexcGatherLoad(MaskHalf, addr, ElementSize, lane)`**, a new natemu IR op (2.2) whose result is the element,
   loaded or not.
3. Insert the elements into two temporary vectors (`_VInsElement` into a zero vector, never into a guest register), then
   blend with the old destination by the mask's sign bits (`VSShrI(mask, ew*8-1)` then `VBSL`), so masked-off elements keep
   the destination's value. The QD/QPS and 128-bit forms zero what the SDM zeroes, as FEX's code does today.
4. Store the result to the destination and zero the whole mask (`AVX128_StoreResult_WithOpSize`), after the last
   `FexcGatherLoad`: the SDM's state when no element faults.

Nothing of the instruction is written before its last element load: dest, mask and the upper halves in the guest-state
page are written only by step 4. So every element's load is at a point where guest state is the state before the
instruction (`EFFECTS_NONE`), which is what the resident needs to retry or step it (2.4).

### 2.2 The JIT op

`FexcGatherLoad FPR:$Mask, GPR:$Addr, u8:$Size, u8:$Lane` (fexc_sites.inc, beside FexcRepStos), emitted as:

```
umov  w0|x0, vMask.s|d[lane]      ; x0 = TMP1, as the REP ops use it
tbz   w0|x0, #31|#63, 1f          ; masked off: no access at all
ldr   s|d<out>, [xAddr]           ; the site: a plain load, FPR class, width 4 or 8
1:
```

`out` is the op's FPR result; when skipped it is not defined and step 3's blend ignores it. The `ldr` is the op's only
memory word, so the recorder finds it as for any load (fexc_sites.inc:1338-1356).

### 2.3 The site

Kind `LOAD`, class FPR, `width` the element size, `base_reg` = `xAddr`, `imm` 0, `completed_effects` `EFFECTS_NONE`, and
**`guest_offset2 = GATHER_ELEMENT` (`0xC000_0002`, new, beside `X87_INT64` = `0xC000_0001`, code_object.rs:119)**: "an
element load of an inline gather". `object::completion` already ignores those top bits; admission exempts `(LOAD, 4|8,
CLASS_FPR)` sites with the marker where it checks `guest_offset2` (admission.rs:703-705). `rmw` keeps its meaning (an
earlier draft overloaded it; three readers would break: admission's `intent()`, fexc.cpp's `RmwName[]` and check_sites.py).
Everything that treats a load site keeps treating it as one; only the places in 2.4 look at the marker.

### 2.4 The resident

- Shadow miss, contended walk: retry the word in place, as for any load (state is before the instruction, the op has
  written nothing).
- UC RAM, a watchpoint, an A/D update on a code page, a dead block: `step_from`, as for any load today. The step is
  compiled without `--bulk`, so fexc cuts the gather and the resident runs `system()` with `gather::transaction` (as FLDCW
  and X87TAG already do, execution.rs:4083-4086).
- **A guest fault** (#PF, U/S, a non-canonical #GP/#SS): one check at the top of `guest_fault` (execution.rs:3581), which
  `translate` (3916-3917) and `access_aborted` (4273-4274) both reach: a `GATHER_ELEMENT` site in a block that is not a
  step goes to `step_from` instead. The SDM requires the elements below the faulting one to be completed before the fault
  is delivered; the transaction does that.
- **Device and mapped pages.** At a `GATHER_ELEMENT` site whose load misses, any page that is not plain RAM (a device page,
  `Class::Mapped`, an unassigned page) steps, and the check comes before `mmio::ones`, so no device access and no all-ones
  shadow installed at that miss completes an element inline. The gather then reaches the transaction and stops or completes
  where it does today (`unsupported_mmio`, `run_mapped.py`'s `gather-element`, `k2-gather`'s `mmio`). Device pages are never
  shadowed, so their elements always take this path. A page already shadowed is not decided here: an unassigned page an
  earlier read shadowed onto the all-ones page, or a mapped region's page shadowed onto the region, is read by the element's
  plain load as any plain x86 load from it reads it in natemu, while the transaction stops `unsupported_mmio` on a mapped
  element. Which one a guest sees depends on shadow history (recorded in gather.rs's header; review, section 7).
- **Termination.** A `GATHER_ELEMENT` site in a `cache::STEP` block is `SITE_MISMATCH` (as the `STEP` guards at
  execution.rs:4074-4086 and 4318), so a step can never step into itself; with 2.5's admission rule it cannot happen.

### 2.5 Admission and the precision checks

- **Bulk objects only.** An object with a `GATHER_ELEMENT` site sets `Admitted.bulk` (today derived from the REP back
  edges, admission.rs:439, structure.rs:1226/1272), so a non-bulk (step) request refuses it (execution.rs:3623). fexc reads
  `fexc_bulk` through its tracked `FexcArg` (fexc_hook.h:253) so answers are keyed by it (execution.rs:6197-6199).
- **The op's shape**, triggered by the marked site (no new branch rules are needed for safety: forward conditional
  branches, `umov` and FPR loads of width 4 or 8 are admitted today): the three words `umov w0|x0, vM.s|d[i]; tbz
  w0|x0, #31|#63, +8; ldr s|d<t>, [xA]`, the `tbz` bit matching the width and skipping exactly the `ldr`, nothing else
  branching into the `ldr`, the guest bytes VEX.66.0F38 90-93 with no 0x67/FS/GS in a 64-bit object, and every site of that
  instruction marked, as many as its elements. Admission cannot prove which lane or mask register the `umov` reads (they
  are temporaries); the checks above and the tests cover that. This pin is part of the code shape, a debug check (off by
  default, on in the test suites); object validation, always on, admits the mark only on a 4- or 8-byte load into a vector
  register, makes a marked object a bulk one and refuses one in a 32-bit object. So with the code shape off, a gather whose
  element sites a producer bug left unmarked is admitted and loses exactness (a fault there is delivered at the gather with
  no partial progress) but not soundness: the loads are ordinary load sites. `x86_gather` recognises the gather's bytes only
  (map, prefix, opcode, VEX.L and W); it does not re-check the #UD encodings (no SIB byte, a register operand, two of
  destination, mask and index the same), which it trusts the producer (fexc_slice1.inc) to have cut.
- `tools/fexc/verify_precision.py` P5 ("no site may be skipped by a forward branch inside its instruction", REP's `cbz x7`
  the only exception) gets the second exception: a `tbz` that skips exactly the next word, a `GATHER_ELEMENT` site's `ldr`.
  `tools/fexc/check_sites.py` gets a gather model (its expected accesses: one load per active element) so gather programs
  can join the corpus.
- P1 (no write to a guest static register or store before a site) needs no exception: 2.1 step 4 comes after every load,
  and FEX's allocator drops static-register coalescing across every guest-memory op (RegisterAllocationPass.cpp:647-661).
  The checker proves it over its corpora (not per compile; admission does not check P1).
- `fexc_precision.h`: `OP_FEXCGATHERLOAD` is a `GuestMemOp`, `LoadsInto` (so P3 holds and the result never gets a static
  register), and in `KeepsStaticFPRs` and `Commits` as a load with side effects.
- **No spills.** An object with a spill area is refused (admission.rs:234-241); a test compiles all 16 forms (with and
  without base and displacement) and checks `spill_bytes == 0`.

### 2.6 What stays the system cut

A step (TF, the `step_from` fallback itself), alignment-gated or data-breakpoint-armed blocks (no `--bulk`), 32-bit code,
0x67, FS/GS, `--avx-gated` blocks (the #UD/#NM gating stays a cut), and every #UD encoding (`fexc_slice1.inc`:705-714,
before the table).

## 3. Why it is exact

- **Elements, order and masks.** Each active element is one plain load of its own address, lowest first, skipped when its
  mask bit is clear (no access, so no fault, for a masked-off element). Completion writes dest (masked-off elements kept)
  and zeroes the mask, which is the SDM's result for a gather that completes.
- **Faults.** No element's load can complete the instruction partially: the guest-visible state at every element site is
  the state before it. A shadow miss is retried. Anything guest-visible steps the instruction through the transaction that
  already defines natemu's gather semantics (gather.rs:12-22, pinned to the AMD reference), so a fault, breakpoint, UC or
  device access ends in exactly the state it does today.
- **Ordering.** Plain loads in element order under the hardware's TSO mode, as every block access; a gather has no
  ordering requirement between its elements beyond that (SDM: element order is not architecturally visible except for faults).
- **Interrupts.** None is taken inside the op; events arm at the next exit, as for any block.

## 4. Tests

- **Both paths, one suite.** A debug setting `[debug] inline_gather` (default true; false makes the host send
  `--no-inline-gather`, fexc then cuts every gather as today, precedent `fexc_no_bulk_movs`) lets `k2-gather` run each
  sub-case twice: inline (default) and cut. The cut run is today's suite unchanged. The inline run asserts the inline path
  ran on every no-fault case: no GATHER decode (`decodes[0x7a] == 0`, today's check at run_k2.py:2680-2681 flips) and no
  step; on fault cases it asserts the step happened and the SDM state.
- Unchanged and green: `run_mapped.py`'s `gather-element` and `k2-gather`'s `mmio` (2.4: device pages step), the FS
  gathers of `compat_limits.py` (cut), the i8 and i10 #UD cases, host `system::tests::gathers`, FEX's 96 ASM gather tests.
- New: `tools/fexc/test_slice1.py`: a gather compiles inline with `--bulk` (words and sites) and stays a cut without it,
  with `--no-inline-gather` and in every 2.6 case; all 16 forms with `spill_bytes == 0`. Host admission tests pinning the
  words and refusing a moved `tbz`, an unmarked site of a gather, and a gather site in a non-bulk object. `k2-gather`
  sub-cases: a fault on a middle element of a gather inside a block of several instructions; element pages not yet
  shadowed (the retry path, with no step); a masked-off element on a present page whose PTE.A is clear, which must stay
  clear (the inline `tbz` skipped it: a load would have walked and set A, and a fault-and-step would hide it).
- Mutations shown to fail: the `tbz` dropped (the PTE.A case), the blend dropped (sparse-mask forms), `guest_fault` instead
  of the step (the partial-progress cases), the termination guard dropped (admission test).

## 5. Measurement

One A/B on the M2 Max (the machine of the experiment) against this branch's base, on the llvmpipe experiment, plus the
benchmarks a gather change can move. The experiment's numbers stay labelled as an experiment; the separate bench-case work
makes them citable.

**No existing bench case can move** (checked November 25): the M4's bench runs at 3b13c1d0, the commit of
[benchmarks.md](../benchmarks.md)'s numbers and the same natemu code as this branch's base (no change under `crates`,
`tools`, `third_party`, `testkit` or `tests/bench` between them), hold 118 `bench.jsonl` files with 116 system-instruction
tables (`kinds`) over every 64-bit and 32-bit case, `guest-avx` included: none counts a single GATHER (kind `7a`). Their
guest code never reaches the changed path, and fexc's output for every other instruction is unchanged, so no bench case
was re-run and the page is unchanged.

## 6. Review

Fable, November 25, 2026 (plan as first written). Blocking, all folded in: (B1) block size 1 is not a step, so it runs
the inline path too; the old path needs its own switch (4). (B2) `k2-gather`'s GATHER-decode assertion flips, and device
and mapped pages must step to keep `gather-element` and `mmio` as they are (2.4). (B3) a wrongly loaded masked-off element
is healed by the step, so tests must prove the inline path ran (no decode, no step, PTE.A) (4). (B4) nothing guaranteed a
step never holds an inline gather: admission's bulk rule and the resident's STEP guard (2.4, 2.5). Non-blocking, folded
in: the marker in `guest_offset2`, not `rmw` (2.3); one interception point at `guest_fault` (2.4); the `fexc_precision.h`
classes; check_sites' gather model; the spill test; admission's pin is a producer-shape check that cannot name lanes.
On a faster system path (a `sys_fast` entry for GATHER, element reads through the shadow): a complement worth perhaps
2-3x per gather against 20-50x inline, only for the cases that stay cut (32-bit, FS/GS, steps); not in this plan.

## 7. Built

November 25, 2026, on `gather-fast-path` (a1fa748c..), FEX fork e7f453e (branch `natemu-gather`, on 934032e). Reviewed by
Fable the same day: no blocking findings; the non-blocking ones are folded in (2.4's shadowed pages, 2.5's code-shape and
#UD notes, the fexc assertions that compile to nothing, the 32-bit refusal's test, the flag names, this section).

**What was built.** Sections 2.1-2.5 as written: the IR op `FexcGatherLoad` and AVX128_VPGATHER's inline lowering in the
fork; the allowlist's verdict (fexc_slice1.inc), the JIT op, its site (`FEXC_GATHER_ELEMENT`), its precision classes and
`--no-inline-gather` in the shim; `code_object::GATHER_ELEMENT` and `gather_element()`; admission's exemption, bulk rule,
32-bit refusal and code-shape pin (structure.rs `gathers`, `x86_gather`); the resident's step at a guest fault and at a
non-RAM page, and its STEP guard; `Compiler::inline_gather` (the CLI's `[debug] inline_gather`, `--set
debug.inline_gather`; `NATEMU_INLINE_GATHER=on|off` for every driver); verify_precision's second P5 exception and its
gather corpus, check_sites' gather model, fexc-replay's `only_inline_gather`.

**Tests.** test_slice1 `GatherInline` (case `fexc-slice1-gather-inline`); host `gather_element_loads_are_pinned`,
`gather_element_loads_are_refused_in_32_bit_objects`, `gathers_inline_only_when_asked`; `k2-gather` runs every sub-case
inline and cut, the inline runs asserting no decode and no step where nothing faults and a step at each fault, plus
`middle`, `fresh` and `pte-a`. Green on the M2 Max: the static fexc cases (precision 0-3 and 32, sites, skips, replay),
`k2-gather` and `mapped-refusals`, the i8 and i10 fault cases, `compat32-resident-limits` and the mapped cases, the Rust
units, and `--tier smoke`.

**Mutations, each shown to fail and reverted:**

| Mutation | Failed |
| --- | --- |
| the `tbz` dropped (shim) | `k2-gather` inline `pte-a` (A set), `user-nc-masked`, `mmio-masked` (a step and a decode); `fexc-slice1-gather-inline` |
| the blend dropped (fork) | 46 inline `k2-gather` sub-cases (forms, fault-*, middle...); no cut one |
| `guest_fault` instead of the step (resident) | 42 inline `k2-gather` sub-cases (fault-*, middle: the partial state); no cut one |
| the bulk rule without the gathers (admission) | host `gather_element_loads_are_pinned`, `gathers_inline_only_when_asked` |
| P5's gather exception removed | `fexc-precision-0..3` (skippable site, `tbz w0, #0x1f`) |
| the `tbz` skipping two words (shim) | `fexc-precision-0..3`; `fexc-slice1-gather-inline` |
| no step at a device or mapped page (resident) | `k2-gather` inline `mmio` (not stepped); `mapped-refusals/gather-element` (halted) |
| check_sites' gather model one load short | `fexc-precision-0..3` |
| the host always sending `--no-inline-gather` | 56 inline `k2-gather` sub-cases (323 gathers decoded, want 3) |

**Departures.** scan_skips.py's tbz/tbnz pattern now matches the hex bit number objdump prints (`#0x1f`): before, no
tbz/tbnz was ever seen skipping a site, so P5's exception would have been unreachable. `inline_gather` is not in the
answers kept in memory (fixed for the process), only in the request (so the persistent cache's key). The code-shape pin
is in shape mode only (2.5). The setting has no CLI flag of its own: `[debug] inline_gather`, `--set` and the variable.
check_sites counts one load per element of the form (sites are static), not per active element.

**An experiment** (M2 Max, natemu e7e8f50c, fexc e7f453e; the recipe in `.natemu/llvmpipe-exp/`, not a framework case):
glmark2 with llvmpipe's AVX2 scores 432 against 191 before (texture 21 -> 952 FPS, desktop blur 1 -> 62, refract 2 -> 21,
jellyfish 3 -> 100, effect2d 7 -> 365); held to SSE4.1, 560 against 543 before (no gathers: unchanged within its noise). A
profile of the texture and blur scenes with the new compiler moved the resident's `system` bucket from 77.8 s to 1.0 s;
the rest is translated code. The numbers stay an experiment until a bench case measures them.
