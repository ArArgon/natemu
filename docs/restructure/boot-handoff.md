# Boot-phase handoff

Status: October 28, 2026, end of the restructuring phase ([vm-restructure.md](../vm-restructure.md), phase boundary).
This page is enough to start the next phase (stock-kernel boot, I5); the slice contracts and the
[contract inventory](contracts.md) hold the detail and the evidence. No performance tuning was done in this phase.

## 1. What exists

x86-64 guest code runs as FEX-compiled ARM64 blocks inside one Hypervisor.framework VM, at EL0 since E2, under a small
resident runtime at EL1 that owns translation, faults, the code cache and system instructions. Every private page is
EL1-only except the code area, which EL0 may only execute; shadows of guest pages are EL0 read/write or read-only. The host compiles and supervises; it
never runs guest code.

| Piece | What it does | Where |
| --- | --- | --- |
| Compiler (fexc) | Native macOS build of FEX's decoder, IR, optimizer, allocator and emitter. Takes x86 bytes, RIP and CPU context; returns ARM64 code with every guest-memory access site, exits, link points and relocations (code object v4; since E1 the code touches no emulator data: exits by `svc`/a relocated `b`, division by `svc`; since E1b no stop check; since E3 no address guard: a non-canonical access faults in hardware at its site). Cuts a block before any system instruction or unsupported form. One `fexc --serve` child process per vCPU. | `tools/fexc/` (build, patch series, shims), `third_party/FEX` (pinned submodule); binary: `../natemu-raw/fexc-native-20261026-01/series22/obj/fexc` (X2: series21-s3, which cut group 9's register forms, plus `--alignment-gated`) |
| Admission | Object validation of each block before it reaches the VM: header, profile, features against the vCPU's ID registers, offsets, sites and their words, relocations, exits and link points, x25/x28 rules, control flow. Since E3 the code-shape check (instruction allowlist, sp, literals, every memory word a site, split/branch-target/#DE check forms) is a debug option (the program driver's `--code-shape`, passed by the test runners except `contain`). | `crates/host/src/admission.rs`, `admission/structure.rs`; format `crates/abi/src/code_object.rs` |
| Host | VM, GIC and vCPU lifecycle (`vm.rs`), guest RAM and the resident image (`memory.rs`), one thread per vCPU with start barrier, cancellation and join-before-destroy (`owners.rs`), the per-vCPU service loop: compile requests, system-instruction decode, kicks (doorbells), watchdog (`execution.rs`, `system.rs`). | `crates/host/src/` |
| Resident | Boots each vCPU's private address space, banks x86 state, walks guest page tables (four-level, A/D by compare-exchange), installs owner-private shadows (native stage-1 tables, one space per x86 privilege class: supervisor and user, each with its own roots and ASID, two ASIDs per vCPU; the CPL's space is loaded at every entry into generated code, U1), places and links blocks, recovers faults precisely, executes system instructions, drains invalidations. | `crates/runtime/src/` (`execution.rs`, `cache.rs`, `tables.rs`, `gates.S`); shared layouts `crates/abi/src/` (`execution.rs`, `paging.rs`, `shadow.rs`, `guest_memory.rs`) |
| Code cache, dispatch, linking | Per vCPU: code area (16 MiB default, up to 128 MiB) with a write alias, metadata arena, L1 (4,096 entries by RIP, as code in the code area since E1) and L2 (key = RIP, CR3, CPL, mode and controls). The dispatcher (copied into the code area) branches into L1 from generated code; a miss enters the resident (`svc`), which looks up L2 or requests a compile. Literal exits go to the linker (`svc`) once and are then patched to branch directly. Overflow or any context change flushes the whole cache. | `runtime/src/cache.rs`, `gates.S` (`dispatch_template`); [slice-2 contract](slice2-contract.md) |
| Precise faults | Every guest-memory access is a registered site. A native abort is matched to its site first (else halt `trapped`, `private_access`, `unknown_abort` or `site_mismatch`); a non-canonical address at a site, the private window included, is #GP/#SS, except the documented guest-state page (`PRIVATE + 0x24000`, one 4 KiB page per vCPU, EL0 read/write since I1: an access there reaches the vCPU's own guest state; boot-features plan, Built (I1, G1 minimum)); the resident walks with the access's intent and retries, splits a page-crossing store, or banks the exact pre-instruction state and stops with the guest fault (vector, error, CR2). | `runtime/src/execution.rs` (`aborted`, `access_aborted`, `split`); [slice-1 contract](slice1-contract.md) |
| Precise stop | Since E1b, by doorbell and unlinking, all in the VM: the HVF in-kernel GIC (macOS 15+) gives each vCPU an SPI (host kicks, `hv_gic_set_spi`) and SGI 1 (peers' residents, `ICC_SGI1R_EL1`). Generated code runs with IRQs unmasked; the resident's IRQ handler (`interrupted`) acknowledges, then stops at once when interrupted in the dispatcher, L1 or at a block's first word, else patches every exit of the interrupted block to `brk #6` (`cache::arm`); the block stops at its exit with the exit's exact state (`stopped`), or at its next resident entry; patched words are restored at every resident entry (`cache::disarm`). No host exit; nothing on the hot path. `hv_vcpus_exit` stays the watchdog/cancellation; `hv_vm_protect` is no longer used. | `runtime/src/execution.rs`, `cache.rs`, `gates.S` `irq_gate`; `host/src/vm.rs` (GIC); boot-features plan §5 |
| System instructions | Host decodes (FEX decoder) once per translation, from the bytes fexc was given; since K2 the resident keeps the decode in its code cache as a record without code (filed, keyed and invalidated as a block of those bytes) and later runs it with no host exit; resident executes as a transaction: MOV to/from CR0/CR3/CR4 (WP, PGE, SMEP), INVLPG, RDMSR/WRMSR EFER; #UD/#GP per SDM, else commit. Every control change and CR3 load flushes the vCPU's cache and shadows; INVLPG drops its 2 MiB range. | `runtime/src/execution.rs` `system`, `host/src/system.rs`; slice-3 §4 S3c |
| Cross-vCPU coherence | Global per-page code and writable masks, one invalidation record and a shadow-reset counter per vCPU (atomic RMWs only). A write to a page with code bits posts the page to the holders and kicks them; marking a new code page shoots down peers' writable shadows synchronously; drains at every resident entry and after placing a block (a killed block steps one instruction). Shadows stay per vCPU: x86 TLB semantics, INVLPG/CR3 per vCPU. | `runtime/src/cache.rs`, `abi/src/execution.rs` (`cache`); slice-3 §1 |
| Ordering | Hardware TSO (ACTLR_EL1.EnTSO, checked at start) for admitted single-register loads and stores; fences as `dmb`; memory XCHG as `swpal` at `swap` sites; since I3 every LOCK read-modify-write as one LSE acquire-release atomic (`ldaddal`, `ldclral`, `ldeoral`, `ldsetal`, `casal`, `caspal`) at `atomic` sites, LOCK CMPXCHG* with `dmb ish` before its CAS. | slice-3 §2 |
| Containment | Any resident halt or host error cancels every vCPU; vCPUs are destroyed before the VM; per-vCPU watchdog (10 s default) reports where it stopped; compile requests have a 30 s deadline; every run prints a `teardown:` line (joined, destroyed, children left). | `host/src/owners.rs`, `execution.rs`; slice-3 §4 S3h |

The only entry point today is the test driver: `natemu-host RUNTIME run-program DESCRIPTION` (`host/src/drivers/`),
which builds tables, bytes and initial state per vCPU from a description and runs them. A boot loader is new work.

**Runners and checks** (all `--fexc FEXC --out DIR`, under `./tools/with-env`; each builds and signs the release host
and runtime; contract-by-contract mapping: [contracts.md](contracts.md), "General-path check per contract"):

| Runner | Checks |
| --- | --- |
| `tools/run_slice1_vm.py` | 64 fixtures and seeded corpora (seed 4242: 44 programs) against pinned QEMU; `--max-block 1` too |
| `tools/run_slice1_faults.py` | 3,443 precise-fault cases (paging cells, access forms, probes) against QEMU prefix runs |
| `tools/run_slice2_vm.py`, `run_slice2_faults.py`, `run_slice2_probes.py` | control flow (64 × 3 block sizes), faults inside loops (846 × 3), code-cache/SMC probes (85) |
| `tools/fexc/upstream_tests/run_asm.py` | FEX's ASM suite (235 admitted tests; 372 since I1, 376 after I7, 443 since I2, 480 since I3, 781 since I5a, 1,603 since I8) |
| `tools/run_slice3_vm.py MODE` | `corpora`, `smp`, `limits`, `kicks`, `system`, `coherence`, `sharedpt`, `litmus`, `contain`, `privilege` (U1), `delivery` (X1a), `events` (X2: interrupt eligibility, shadows, POPF, HLT, NMI, #AC; QEMU x2APIC programs, natemu-only cases, random-time stress) |
| `tools/run_i6_faults.py` | I6 x87: gating (#NM), precise faults with the x87 state unchanged, refusals, FXAM classes, FIST indefinite, MXCSR isolation, Linux's FPU probes (SDM rules; `--max-block 1` too); the x87 QEMU corpus is `tools/reference/gen_i6.py` + `ref.py` + `run_slice1_vm.py` |
| `tools/run_k2.py` | K2 resident system path: cost per CLI/STI/POPF/SWAPGS/RDTSC/CPUID/WRMSR/FLD+FSTP m80 loop iteration (decode exits must not grow with N), RDRAND beyond the pool, cross-modifying code at a system instruction (the x86 protocol: after the writer's flag and a CPUID the new instruction runs, every rewrite checked; deterministic) |
| `tools/run_x3.py` | X3 local APIC: register read-backs (xAPIC MMIO, x2APIC), priority/TPR/EOI with nested self IPIs, CR8, the x2APIC #GP rules and x2APIC IPIs at 2-4 vCPUs vs QEMU and the SDM model; xAPIC IPIs at 2/4 vCPUs, one-shot/periodic/masked/HLT timers (exact model from TSC and CNTFRQ), random IPI + timer stress at 2/4 vCPUs, HLT across peers (the driver's end rule), driver fault and typed-stop cases; `--only cost` for APIC access costs |
| `tools/run_m1.py` | M1 guest RAM: tables and data in RAM's top MiB at 4 MiB, 2 GiB and 3 GiB, the device page at R, placement across 0xA0000, cross- and self-modifying code at 0x7ff00000; any runner runs at another size or placement with `NATEMU_RAM`/`NATEMU_PLACE` |
| `tools/run_m3.py` | M3 device pages: MOV-family corpus vs QEMU (on RAM) and `tools/models/mmio_model.py`, fault priority, refused forms, exactly-once under kicks at 1-4 vCPUs, a tampered reply, cross-modifying code at a device access (needs series23-m3) |
| `tools/run_l1.py`, `tools/run_l1_smoke.py` | L1 boot-protocol loader: Debian and synthetic bzImages judged by `tools/models/bootproto.py`, every header and loader refusal with the model's codes (no VM); the smoke test (Debian decompressor to its jump into vmlinux vs QEMU) runs in-tree at 2 GiB since M1 (boot-features plan, Built (L1), Built (M1)) |
| `tools/run_l2.py`, `tools/run_l2_iasl.py` | L2 ACPI tables at N = 1..4 from load-only dumps: the independently written checker `tools/models/acpi_model.py` (structure, AML, CPUID/LAPIC/e820/device-set agreement; controls in `test_acpi_model.py`), and ACPICA `iasl` (Homebrew `acpica`): `-d` clean except the version-0 wakeup structure, the DSDT recompiled from its disassembly and the RSDP from iasl's template byte-equal (no VM; boot-features plan, Built (L2)) |
| `tools/run_l3.py` | L3 port I/O, UART and the unassigned rule (programs `tools/bench/smp/ports.py`): `qemu` UART widths, Linux's 8250 probe and earlyprintk sequences, unassigned ports, REP INS/OUTS with faults midway, CPL 3 IOPL/TSS-bitmap configurations vs pinned QEMU (COM1 = `-serial file`); `stress` events between string elements; `cases` strict switch, PCI probes, wrap, refusals; `pages` unassigned pages (hole, above RAM, above 4 GiB, IOAPIC page) on the all-ones shadow, writes, refusals, strict, SMP, the xAPIC page (boot-features plan, Built (L3)) |
| `tools/run_l4a.py` | L4a I/O APIC, 8259 pair and routing (programs `tools/bench/smp/irqs.py`): `qemu` register read-backs, IRQ4 edge and level (Remote IRR, TMR, EOI broadcast, masked -> unmasked, EOI register), the 8259 (Linux's probe and init, OCW3, ELCR, ExtINT via LINT0, poll, auto-EOI, rotation), the TMR across triggers, destinations at 2-4 CPUs, all vs QEMU and the model; `xapic` destinations in xAPIC mode, LINT0 fixed and the disabled-APIC pass-through; `stress` asynchronous edge and level lines at 4 and 2 CPUs; `halt` the busy-guard veto and its negative control; `lint1` the NMI input |
| `tools/run_l4b.py` | L4b PIT/61H, RTC, ACPI PM block, S5, reset register (programs `tools/bench/smp/timers.py`): `qemu` PIT (latches, access modes, 61H, Linux's `pit_calibrate_tsc` and `quick_pit_calibrate`, IRQ0 rate, PIT-disable sequence), RTC (formats, SET, rollover, UIP, periodic/update/alarm IRQ 8), PM (PM1, PM timer, SCI) and S5 vs QEMU and the exact model, PIT modes 1/4/5 and the RTC's period change (`l4b-more`); `halt` the PIT as a vetoing source; `stress` IRQ0 + IRQ8 broadcast with HLT at 1/2/4 CPUs; `cases` typed stops, refusals and the review's `frozen` probe; `modes` PIT modes 6/7 and the lost-tick policies (reinject/discard per source); `clocks` PM/PIT/LAPIC/RTC against the TSC over 10 s |
| `tools/run_i8_faults.py`, `tools/reference/gen_i8.py` | I8 AVX/AVX2/FMA/F16C: the QEMU 7.2 corpus (`gen_i8.py`, VEX.128 and VEX.256 forms, every rounding mode, FTZ/DAZ; run with `run_slice1_vm.py`), and the fault matrix by the SDM rules against the run cut before the instruction: #PF on 32-byte crossings with no partial update, alignment #GP (16/32), non-canonical #GP/#SS, #UD/#NM gating (OSXSAVE, XCR0, TS; EM/OSFXSR/BMI not gated; legacy prefixes before VEX; XSETBV re-keying), refusals, device and all-ones pages, the XSAVE round trip with live YMM state, DE/MXCSR hand cases (boot-features plan, Built (I8)) |
| `tools/run_i5b.py` | I5b FXSAVE/FXRSTOR, XSAVE/XRSTOR, XGETBV/XSETBV: `images` (FXSAVE/XSAVE/XRSTOR images and XCR0/CPUID.0DH sequences byte by byte against QEMU's Haswell model, block sizes default and 1, declared differences compared on natemu's side), `faults` (alignment, #PF at page boundaries with no partial update, #NM/#UD gating, XSETBV/XGETBV and XRSTOR header #GP, refusals, #AC never; SDM rules, QEMU where it can run the case) (boot-features plan, Built (I5b)) |
| `tools/check_resident_stack.py` | resident stack bound and gate/ERET binary shape of the built runtime |
| `tools/models/` | independent ordering, coherence, PAE and modes models (used by the runners) |

Full suite (about 10 minutes on the M4 at the phase end; the reduced `kicks`/`sharedpt`/`litmus` settings below are
the phase-end ones):

```sh
# From the repository root; works in zsh and bash (no word-splitting of variables; run in full under zsh and
# argument-checked under bash at the phase end).
F=../natemu-raw/fexc-native-20261026-01/series30/obj/fexc   # tools/fexc/build.sh rebuilds it; VEX AVX/AVX2/FMA/F16C forms (I8) need series29, its review fixes (HADD order, UNORD flags, VLDMXCSR, the upper-half cache) series30; instructions straddling a page end and over 15 bytes (#GP) need series28; CS/DS/ES/SS prefixes by Intel's class (Linux's `ds btr`) need series27; I5b's FXSAVE/XSAVE cuts need series26-i5b or later; S3's `system` programs (RDRAND/RDSEED/RDPID) need series21-s3 or later, X2's BT* #AC cases series22; since I6 the host passes x87 arguments; series25 is the combined I6 + M3 build
SEED=../natemu-raw/s1d1-seed4242
FLOW=../natemu-raw/s2d/corpus
O=../natemu-raw/suite-$(date +%Y%m%d-%H%M%S)                  # a fresh output directory
s3() { tools/with-env python3 tools/run_slice3_vm.py --fexc "$F" "$@"; }
cargo +1.97.0 build --offline --locked
cargo +1.97.0 test --offline --locked
cargo +1.97.0 build --offline --locked --release -p natemu-runtime --target aarch64-unknown-none
python3 tools/check_resident_stack.py
python3 -B -m unittest discover -s tools -p 'test_*.py'
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/fix"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/fix-1" --max-block 1
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/seed" "$SEED/programs.json" "$SEED/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/seed-1" --max-block 1 "$SEED/programs.json" "$SEED/expected.json"
tools/with-env python3 tools/run_slice1_faults.py --fexc "$F" --out "$O/faults"
tools/with-env python3 tools/run_slice1_faults.py --fexc "$F" --out "$O/faults-1" --max-block 1
tools/with-env python3 tools/run_slice2_vm.py --fexc "$F" --out "$O/flow" --programs "$FLOW/programs.json" --expected "$FLOW/expected.json"
tools/with-env python3 tools/run_slice2_faults.py --fexc "$F" --out "$O/loopfaults"
tools/with-env python3 tools/run_slice2_probes.py --fexc "$F" --out "$O/probes"
tools/with-env python3 tools/fexc/upstream_tests/run_asm.py --fexc "$F" --out "$O/asm" --jobs 4
s3 --out "$O/corpora" corpora --parallel 4
s3 --out "$O/smp" smp --parallel 2
s3 --out "$O/limits" limits
s3 --out "$O/kicks" kicks --kicks 1000 --cpus 1 2 4
s3 --out "$O/system" system
s3 --out "$O/coherence" coherence --reps 1 --parallel 2
s3 --out "$O/sharedpt" sharedpt --reps 1 --parallel 2
s3 --out "$O/litmus" litmus --k 5000 --runs 1 --parallel 2
s3 --out "$O/contain" contain
s3 --out "$O/privilege" privilege
s3 --out "$O/delivery" delivery                               # X1a: nested faults, #DF, shutdown, gates (delivery_model)
s3 --out "$O/events" events --kicks 2000                       # X2: eligibility, shadows, POPF, HLT, NMI, #AC, stress (delivery_model)
tools/with-env python3 tools/run_i6_faults.py --fexc "$F" --out "$O/i6-faults"                  # I6: x87 gating, faults, refusals
tools/with-env python3 tools/run_i6_faults.py --fexc "$F" --out "$O/i6-faults-1" --max-block 1
tools/with-env python3 tools/run_m3.py --fexc "$F" --out "$O/m3"   # M3: device pages (QEMU via Docker, mmio_model, kicks)
python3 tools/run_l1.py --out "$O/l1"                        # L1: loader vs bootproto.py, refusals (load-only, no VM)
python3 tools/run_l2.py --out "$O/l2"                        # L2: ACPI tables vs the independent checker, N = 1..4 (load-only, no VM)
python3 tools/run_l2_iasl.py --out "$O/l2-iasl"               # L2: the same tables through iasl -d, DSDT and RSDP recompiled
tools/with-env python3 tools/run_k2.py --fexc "$F" --out "$O/k2"   # K2: system-instruction cost, RDRAND refill, XMC at system instructions
tools/with-env python3 tools/run_x3.py --fexc "$F" --out "$O/x3"   # X3: local APIC, IPIs, timers vs QEMU and the SDM model; stress; HLT across peers
tools/with-env python3 tools/run_m1.py --fexc "$F" --out "$O/m1"   # M1: RAM top at 4 MiB/2 GiB/3 GiB, the holes, code near the RAM top
tools/with-env python3 tools/run_l3.py --fexc "$F" --out "$O/l3"   # L3: port I/O, UART, IOPL/bitmap vs QEMU; unassigned ports and pages (all-ones shadow), strict switch
tools/with-env python3 tools/run_l4a.py --fexc "$F" --out "$O/l4a"   # L4a: I/O APIC, 8259, routing vs QEMU; async lines, the halted veto, LINT0/LINT1
tools/with-env python3 tools/run_l4b.py --fexc "$F" --out "$O/l4b"   # L4b: PIT, RTC, PM block, S5/reset vs QEMU; halted veto, stress, typed stops, clock agreement
tools/with-env python3 tools/run_i5b.py --fexc "$F" --out "$O/i5b"   # I5b: FXSAVE/XSAVE images vs QEMU (Haswell model), the fault matrix by the SDM rules
I8=../natemu-raw/i8-corpus-20261030-01                             # I8: gen_i8.py programs and their QEMU 7.2 results
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/i8" "$I8/programs.json" "$I8/expected.json"
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/i8-1" --max-block 1 "$I8/programs.json" "$I8/expected.json"
I8B=../natemu-raw/i8-corpus-20261030-02                            # I8 review: every template k = 2 times per width and group, legacy SSE mixed in, shifts/flags/addressing kinds
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/i8b" "$I8B/programs.json" "$I8B/expected.json"          # 7 known QEMU two-NaN departures (plan, Built (I8) review)
tools/with-env python3 tools/run_slice1_vm.py --fexc "$F" --out "$O/i8b-1" --max-block 1 "$I8B/programs.json" "$I8B/expected.json"
tools/with-env python3 tools/run_i8_faults.py --fexc "$F" --out "$O/i8-faults"               # I8: AVX faults, gating, refusals, XSAVE round trip, DE
tools/with-env python3 tools/run_i8_faults.py --fexc "$F" --out "$O/i8-faults-1" --max-block 1
```

QEMU references run in the pinned container image (`tools/reference/ref.py` holds the pins) or Homebrew
`qemu-system-x86_64` for SMP programs; the no-Linux-VM rule covers compilation only.

## 2. Supported subset and declared limits

| Area | Supported | Not supported (refused, never silently wrong) |
| --- | --- | --- |
| Mode | 64-bit long mode, CPL 0 and 3 | 32-bit, compatibility, paging off, real mode (owner decision 3); non-flat segments |
| ISA | Integer, loads/stores 1–8 bytes at any alignment, control flow (incl. `loop`/`jrcxz`, indirect, call/ret), 128/64 division (resident helper), LFENCE/MFENCE/SFENCE, memory XCHG, PAUSE; since I1 CLC/STC/CMC, CLD/STD (DF in the guest-state page), LAHF/SAHF, PUSHF, ENTER (level 0)/LEAVE, SHLD/SHRD (32/64), CMPXCHG/XADD without LOCK, POPCNT/TZCNT/LZCNT, BMI1/BMI2 but PDEP/PEXT, PREFETCHh, MOVNTI; since I7 FS/GS overrides on memory operands (base + EA, bases in the bank) and CS/DS/ES/SS prefixes naming the default segment; since I2 MOVS/STOS/LODS/SCAS/CMPS alone and with REP/REPE/REPNE, one element per pass (boot-features plan, Built (I2)); since I3 every lockable memory form with LOCK (one LSE atomic; LOCK NEG one compare-and-swap per pass) and CMPXCHG8B/16B locked or not, LOCK elsewhere #UD (boot-features plan, Built (I3)) | SSE/AVX/x87, 32-bit-address (0x67) and REPNE-on-move string forms, LOCKed accesses crossing 16 bytes (split locks, also an unlocked CMPXCHG8B: `unsupported_atomic`), F2/F3 with LOCK and 66/F2/F3 on CMPXCHG8B/16B, FS/GS with no memory operand or a second segment prefix, CS/DS/ES/SS naming a non-default segment (`unsupported_form` / `unsupported_atomic`) |
| System | MOV CR0 (WP), CR3, CR4 (PGE, SMEP), MOV from CR0/CR3/CR4, INVLPG, RDMSR/WRMSR EFER | Everything else: §3 (`unsupported_system`, #GP or #UD per the model) |
| Paging | Four-level, 4 KiB and 2 MiB pages, WP, NXE, SMEP, A/D per SDM with a declared fault-path policy (leaf A only after the permission check) | 1 GiB pages, PCID, SMAP, LA57, PWT/PCD/PAT memory types (halt), MAXPHYADDR above 40 |
| vCPUs | 1–4 (`MAX_VCPUS`). N = 0 cannot be described; `execution::prepare` refuses it (unit test); 5 is refused (`limits`) | APIC, IPIs, INIT/SIPI |
| Memory | Guest RAM of a run-time size since M1 (2 MiB multiples to 3 GiB, default 2 GiB, `--ram`); 288 shadow-table pool pages per vCPU (reset on exhaustion, counted); since M3 device pages above RAM: MOV-family loads and stores of 1-8 bytes completed once from their sites' completion records through the host's device registry (fexc series23-m3; boot-features plan, Built (M3)) | Other forms on device pages (`unsupported_mmio`), holes; host writes only with every vCPU stopped |
| Faults | Precise guest faults reported as a stop (vector, error, CR2, banked state) | Delivery to the guest (no IDT); the run ends at the fault |
| Atomicity | Aligned 1–8 bytes and unaligned within 16 bytes single-copy atomic | Non-LOCKed access crossing 16 bytes inside a line may tear for peers (owner decision 2) |

### ISA coverage measured (October 28, after the phase)

Admission takes about 151 of 256 primary-map slots and about 70 two-byte slots, all integer: roughly 85–90% of the
legacy integer ISA, under 10% of all x86-64 forms. Refused entirely: SSE/MMX, AVX, x87, string and REP, every LOCK form
but memory XCHG, `popf`, CPUID, RDTSC. Since I1 (series14) admitted: the flag moves, `lahf`/`sahf`, `pushf`, `enter`
(level 0)/`leave`, `shld`/`shrd` (32/64), `cmpxchg`/`xadd` without LOCK, `popcnt`/`lzcnt`/`tzcnt`, BMI1/BMI2 but
`pdep`/`pext`, `prefetch`h, `movnti` (the ISA row above); still refused of these families: `pdep`/`pext`, `prefetchw`,
`cmpxchg8b`/`16b`, 16-bit `shld`/`shrd`, `enter` with a nesting level, 66-prefixed `pushf`/`enter`/`leave`.

| Measure | Result |
| --- | --- |
| FEX ASM suite | 235 / 2,270 pass (10.4%), 0 fail (I3, series17: 480 pass, 0 fail; 45 split-lock stops (FEX's Atomics tests cross 16 bytes on purpose) and 6 cmpxchg8b/16b with 66/F2/F3 refused. I1, series14: 372 pass, 0 fail; remaining I1-class refusals: 16-bit shld/shrd 19, cmpxchg8b/16b, popf, o16 enter/leave, enter with a nesting level). First refused instruction of the 2,035 unsupported tests: AVX 726, SSE/MMX/AES/SHA 480, x87 401, flag moves 205 (mostly test set-up), LOCK/cmpxchg/xadd 90, system 41, BMI 38, shld/shrd 17, other 37 |
| Static share of admitted instructions in macOS x86-64 binaries | bash 97.5%, ls 97.7%, ld 92.3%: the rest is almost all SSE (`movaps`/`movups`/`xorps` from compiler copies and zeroing, scalar SSE float), present in nearly every function, so most real functions fail |

Suggested order for the ISA track: flag moves and small integer forms (cheap), REP strings with partial progress (44),
LOCK forms, CPUID/RDTSC and system registers, SSE (state in the frame, vector sites in admission/recovery, ordering
cells, OSFXSR/XSAVE), then x87 (FEX's soft-float helpers must run in the resident) and AVX (only if CPUID advertises it).

## 3. Boot-phase prerequisites and handoff items

| Item | State now | Origin |
| --- | --- | --- |
| Interrupt and exception delivery: IDT, IRET, error-code push, vector priority, IF/interrupt shadow; IPIs (cross-vCPU INVLPG/TLB shootdown is guest software over IPIs) | Faults stop the run; no IDT; no IPIs | contract 49; slice-3 §1 ("IPIs: boot phase") |
| 64-bit FS/GS bases (Linux per-CPU data via `%gs`), SWAPGS, KERNEL_GS_BASE, STAR/LSTAR/SFMASK, PAT, TSC/APIC-base and other MSRs Linux reads | Every MSR but EFER #GPs; FS/GS overrides admitted since I7 (bases from the bank, set by the driver's `fsbase`/`gsbase` until S3) | owner decision 1 note; S3c1 review |
| MOV from CR2 (the #PF handler reads it); MOV CR8 | Refused `unsupported_system` | S3c1 review |
| CR4 bits Linux sets: OSFXSR, OSXMMEXCPT, OSXSAVE, FSGSBASE, UMIP; CR0 TS/MP/NE/AM changes; CR4 profile bits (VMXE, SMXE, PKE, UMIP) must become reserved per the CPUID profile | Refused today | S3c1 review |
| LGDT/LIDT/LTR (and SGDT/SIDT/STR, segment register loads) | Cut as system instructions by fexc, refused by the host decode (`unsupported_system`) | slice-1 contract S1c (system classes); `host/src/system.rs` decodes only MOV CR, INVLPG, RDMSR/WRMSR |
| CLI/STI/HLT (IF and the interrupt shadow, halt until an interrupt) | Cut and refused; `hlt` ends a test program | slice-1 contract S1c; contract 49; slice-2 contract S2f (`hlt` ends a test) |
| SYSCALL/SYSRET (with STAR/LSTAR/SFMASK; SWAPGS) | Cut and refused | slice-1 contract S1c |
| RDTSC/RDTSCP (a TSC source and its relation to the timer) | Cut and refused | slice-1 contract §2 ("CPUID/RDTSC ... are cut") |
| IN/OUT (port I/O for the console and legacy devices) | Cut and refused | slice-1 contract S1c; devices: phase boundary |
| MOV to/from debug registers | Refused by the host decode | `host/src/system.rs` (test `unsupported_forms_are_refused`) |
| TF single-step and debug breakpoints: dispatch and block linking must be bypassed while TF or a breakpoint is active (FEX's CheckTF) | Not implemented: no TF handling, no #DB | slice-2 contract §2 (dispatcher) and S2f boot-phase notes |
| CPUID, SERIALIZE and IRET as draining entries (cross-modified code is seen at a serializing instruction); CPUID must report Page1GB = 0, LA57 = 0 and only what is implemented | CPUID ends a block and is refused (`unsupported_system`) | slice-3 §1, S3a2 walker note |
| Memory types: the walker treats any PWT/PCD/PAT bit as non-write-back and halts (`shadow status=1 detail=7`); evaluate PAT first. No 1 GiB pages | Refusal | S3a2 "Walker refusals" |
| LOCKed accesses crossing a 16-byte boundary (split locks; an unlocked CMPXCHG8B too) | Stop `unsupported_atomic` before any effect (owner decision); every other LOCK form built in I3 | slice-3 §2, S3e, boot-features plan Built (I3) |
| REP string operations with partial progress (RCX/RSI/RDI per element, RIP at the REP) | Not admitted | contract 44 |
| LFENCE register capture (the old completion sink); SSE/x87/AVX state and instructions | LFENCE admitted as `dmb ld` only; no vector state | contract 51; slice-3 §3 |
| 32-bit and paging-off modes, compatibility mode, far transfers, descriptor loads, legacy/PAE walking | Deleted; start from [s3g-plan.md](s3g-plan.md) (transaction list S3g1–S3g3); the legacy walker `crates/abi/src/paging32.rs` is in commit 4902ee5; the 32-bit families in `8d529f9` | owner decision 3 |
| Segment bases and limits, A20, 16-bit code, device/hole resolver | Deleted; build only if the boot path needs them | owner decision 1 |
| Tearing across 16 bytes inside a line | Documented deviation; a diagnostic may count such stores | owner decision 2 |
| A guest access to the guest-state page (`PRIVATE + 0x24000`, non-canonical: #GP on x86) | Documented deviation: it reaches the vCPU's own guest state; one 4 KiB page per vCPU, straddles into it fault; the resident reads back only DF (since I7 it also writes the FS/GS bases there at every entry). Since I6 the x87 state lives there (I6 deviation 12) and since I5b the AVX upper halves (YMM_Hi128, `frame::AVX_HIGH` C0H, outside `GUEST_STATE`): a guest data access to the page reaches that vCPU's own copy | G1 minimum (I1); boot-features plan, Built (I1, G1 minimum) |
| Kernel not pinned: Debian 6.12.107 (64-bit boot protocol, ACPI-mailbox APs: no 32-bit or paging-off instruction runs with LA57 = 0) or Ubuntu 6.8 (its AP trampoline always goes 64 → compatibility → paging off → PAE/LME/PG → 64, so it needs the 32-bit items and LOCK BTS) | Choose first | s3g-plan.md §1, owner decision 3 |
| Guest RAM beyond 4 MiB, a loader (bzImage, boot params, initramfs), devices, timer (APIC timer or TSC deadline), console (serial) | None | phase boundary |
| Host writes to guest RAM (DMA, loader) | Only with every vCPU stopped; each sets FLUSH in every record | slice-3 §1 |

## 4. Known performance and limits from this phase

No tuning was done (owner, October 27). Latest measurement (S2g, October 27; tools/bench, two back-to-back pairs,
single vCPU) against `qemu-system-x86_64` TCG on the same host:

| Workload | natemu time vs QEMU TCG |
|---|---|
| int | 0.97× |
| mem64 | 0.29× |
| mem4096 | 2.59× |
| call | 0.21× |
| branchy | 0.70× |
| mixed | 0.29× |

mem4096's slowdown follows its power-of-two stride (cache-set conflicts, also native); a VM-only extra of 2–4 ns per
iteration is unexplained. Known costs and limits:

- **`ret` goes through the dispatcher:** about 15 dispatcher words per `ret` (call: 6.0 words per guest instruction;
  the dispatcher is about 20% of `call`). Return-address prediction or `ret` linking would remove it.
- **Data on a running code page:** a peer storing continuously to data on the page a vCPU runs from makes that vCPU
  step every instruction (each shootdown is answered by the next store): `data@4` takes about 120,000 steps for 20,000
  loop rounds.
- **Walker CAS starvation:** a CPU storing to a PTE with no pause at all starves the walker's compare-exchange to about
  200 walks/s; the watchdog contains it. x86's locked A/D update cannot be starved this way; no kernel stores this fast.
- **Host round trip per system instruction** (until K2: since then one decode per translation, then a resident round
  trip, about 0.4 us; boot-features plan, Built (K2)), and a **full cache and shadow flush per CR3 load** (no PCID): correct,
  costly on context switches.
- **INVLPG leaves spurious code bits:** it drops the vCPU's blocks of the 2 MiB range but keeps their pages' code-mask
  bits, so later writes there still take the code-write path until the next flush (conservative).
- **Stop latency tail:** since E1b a stop is a GIC doorbell (no `hv_vm_protect`); it takes effect at the interrupted
  block's next boundary. Ring to stop, hot loop (E2, `kicks --kicks 1000`): p50 5–8 µs, p99 9–59 µs, with a
  host-scheduling tail to about 5–8 ms (max per vCPU, 1 and 4 vCPUs).
- **Doorbell ring cost:** `hv_gic_set_spi` takes 0.4 µs with one kicker and 5.5–6.9 µs on average with four kicking at
  once (same run); acknowledging an IRQ in the resident (IAR to EOI) about 2.2–4 µs.
- **Cancellation during a compile stall:** cancellation does not interrupt a pending compile request; a vCPU whose
  compile server is alive but silent is released only at the request deadline (30 s default, `--compile-deadline-ms`).
  A resident wait on a peer also counts that peer's compile time into the waiter's watchdog span.
- **Resident stack:** 14,176 bytes at the deepest gate path, 19,616 with one nested entry to its halt, of a 32 KiB
  exception stack (13,152 bytes margin; boot path 7,728 of 16 KiB; `tools/check_resident_stack.py`, E2). New resident paths (delivery, devices) must be
  re-checked. An overflow is contained to the vCPU but overwrites its boot/block stack before it halts.
- **Compile latency:** every distinct block is a host round trip on first use (about 0.4 ms per block with the compile
  server, S2a; 13.4 ms the slowest measured, including the server's start).

## 5. Open questions for the owner or the next phase

1. Which kernel: Debian 6.12.107 (no 32-bit code on the planned boot) or Ubuntu 6.8 (32-bit AP trampoline, LOCK BTS)?
   This decides whether S3g's 32-bit work returns.
2. Exception delivery design: deliver in the resident (IDT walk, stack switch, IRET) as a system transaction, with
   interrupts recognized only at block boundaries and drains? IPIs between vCPUs then need a host or resident path.
3. Guest RAM size and layout: 4 MiB today; the boot needs at least tens of MiB, a firmware map and possibly holes or
   MMIO, which the deleted resolver (owner decision 1) handled.
4. Which CPUID profile to report (Intel vendor profile, no LA57, no PCID, no 1 GiB pages, no SSE until it is admitted?)
   and how Linux behaves without SSE/XSAVE (it requires SSE2 on x86-64, so SSE admission is likely the first ISA item).
5. Whether the dispatcher `ret` cost and the per-CR3 full flush (both measured, not tuned) need work before boot timings
   matter, or stay deferred with the performance rule.
