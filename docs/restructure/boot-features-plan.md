# Boot-features phase plan

Status: proposal, October 28, 2026, revised after independent review (ACCEPT-WITH-FIXES; read-only design, no code
changed). Scope is the owner's next-phase direction ([vm-restructure.md](../vm-restructure.md), "Next phase scope",
October 28): build every feature a stock-kernel boot needs, each checked on the general path at this project's bar;
the end-to-end boot itself is **not** in scope; x87 may use FEX's reduced-precision mode. Starting point:
[boot-handoff.md](boot-handoff.md) (H), the [slice-3 contract](slice3-contract.md) §5 decisions D1–D3, the
[S3g survey](s3g-plan.md), the [contract inventory](contracts.md), [implementation.md](../implementation.md) I5.

**Sources and citation keys.** `K file:line` = upstream Linux **v6.12.107** (`gregkh/linux` tag, the Debian target's
base; Debian patches do not touch the trampoline/head paths, INV-01-34 claim 09; other files were not diffed against
Debian's patch set). Paths are relative to `arch/x86/` unless they start with `drivers/`. Reading copies:
`/private/tmp/claude-501/-Users-liaozping-orca-natemu/025ed521-9978-4fb2-aaf7-b90da95d37f3/scratchpad/s3g0/src/`
(the S3g survey's 11 files) and
`/private/tmp/claude-501/-Users-liaozping-orca-natemu/025ed521-9978-4fb2-aaf7-b90da95d37f3/scratchpad/bootsrc/`
(47 files, 1.1 MB, fetched October 28 from `raw.githubusercontent.com/gregkh/linux/v6.12.107/`; file name = full path
with `/`→`_`). Records: `CPU n` = [INV-03-01](../research/investigations/INV-03-01.md) CLM-03-01-n (accepted virtual CPU:
9 MSR dispatch, 12 control transactions, 13 TSC/crystal/LAPIC rates, 16 Intel profile, 17 MISC_ENABLE and microcode);
`M n` = [INV-03-03](../research/investigations/INV-03-03.md) CLM-03-03-n (17/18 early timer accesses, 20 legacy clock
island, 22 S5, 23 IRQ wiring, 24 table finalization, 29 virtual time, 60 COM1, 61 timer arm, 64 NMI/INIT, 69 boot
manifest, loader and map); `B n` = [INV-01-34](../research/investigations/INV-01-34.md) claim n (mailbox); `P2` =
[privilege study](../../experiments/track03/el0-study/README.md). `SDM` = Intel SDM by volume and chapter title.
Ubuntu 6.8 is surveyed only for its AP trampoline (s3g-plan §1).

## 1. Requirements inventory

Boot path (M69, s3g-plan §1): bzImage loaded by our host, BSP entered at `K+0x200` (decompressor `startup_64`) in long
mode with LA57=0, flat CS 0x10/DS 0x18, IF=0, RSI = `boot_params`; APs started through the ACPI MADT version-0
mailbox; Debian 6.12.107; console `earlycon=uart8250,io,0x3f8,115200n8 console=ttyS0,115200n8` (M69); an external
initramfs with `/init` (implementation.md I5); the CPU profile of CPU 16 unless Q3 amends it. With this path no 32-bit
or paging-off instruction executes (s3g-plan §1). **Chunk** refers to §3; the R1 column of §4 maps every row to a
composite or a named check.

### 1a. Exceptions, interrupts, APIC, timers, IPIs

| Feature | Kernel use (cited) | General path must | Check (reference, see §3 fault table) | Chunk |
| --- | --- | --- | --- | --- |
| Exception delivery through a 64-bit IDT: gates, error codes, CR2 | Decompressor maps memory on demand from its #PF handler, reading CR2 (`K boot/compressed/idt_64.c:59-64`, `ident_map_64.c:361-387`); the kernel's early #PF builds page tables (`K kernel/head64.c:362-366`, `head_64.S:371,503-537`); other early exceptions go to `early_fixup_exception` (`head64.c:375`), so `rdmsr_safe`/`wrmsr_safe` probes need a delivered #GP (`K kernel/cpu/intel.c:528,538,997`); WARN/BUG are `ud2` → #UD (`K kernel/traps.c:338-347`) | Today a guest fault *stops* the run (H §2). Delivery becomes a resident transaction: IDT walk through the guest tables, gate checks, frame push, exact pre-instruction state; faults during delivery per SDM | Vector, error code, CR2: SDM models; frame and IRET mechanics: QEMU (S3r stub) where agreeing with the delivery model | X1a |
| CPL3→CPL0 stack switch (TSS.RSP0) and IST | #DF IST0, NMI IST1, #DB IST2, #MC IST3 (`K kernel/idt.c:86,103,105,108`; `include/asm/page_64_types.h:27-31`); `tss_setup_ist`, `load_TR_desc` (`K kernel/cpu/common.c:2264-2278`) | Stack choice from TSS RSP0/IST n; TR from LTR (S2b) | Delivery model; QEMU frames | X1b |
| IRETQ (same CPL and to CPL3), NMI unblocking | Every return (`K entry/entry_64.S:659`); NMI nesting (`entry_64.S:1325,1356,1472`); `sync_core()` = `iret_to_self` (MOV from SS/CS, PUSHF, IRETQ) because SERIALIZE is not advertised (`K include/asm/sync_core.h:22-37,58-88`) | IRET transaction with frame checks, CS/SS reload, RFLAGS incl. IF, NMI unblock even if IRET faults (M64); a draining entry (slice-3 §1) | Delivery model; QEMU frames | X1a, X1b |
| #DF classes, nested faults, triple fault | `exc_double_fault` (`K kernel/traps.c:454`) | SDM double-fault table; a fault while delivering #DF = shutdown: typed VM stop | Delivery model only (QEMU resets) | X1c |
| INT3, INT n, INTO, UD2 | Boot-time `int3_selftest` (`K kernel/alternative.c:1829-1855`); runtime patching via INT3 (`alternative.c:2462-2482`) | Software interrupts as transactions (gate DPL vs CPL) | Delivery model; QEMU frames | X1a |
| Paranoid entry reads the CPU number with **LSL** | `setup_getcpu` writes the CPUNODE descriptor (limit = CPU/node, `K kernel/cpu/common.c:2208-2226,2270`); with FSGSBASE the paranoid entry of NMI/#DF/#DB/#MC runs `SAVE_AND_SET_GSBASE` (`K entry/entry_64.S:898,909,1426`) → `GET_PERCPU_BASE` → `lsl` (`K entry/calling.h:401-420`); vDSO getcpu at CPL3 also uses `lsl` when RDPID is absent (`K include/asm/segment.h:245-259`) | LSL: descriptor fetch, type/DPL checks, ZF, limit (G-scaled) | SDM model; QEMU programs | S3, X1b |
| IF, CLI/STI shadow, POPF, MOV SS shadow, HLT | Idle = `default_idle` → `raw_safe_halt` (`K kernel/process.c:751-755`; MWAIT not advertised (CPU 16), `process.c:884,947`); decompressor `cli` (`K boot/compressed/head_64.S:299`); `popfq` (`K kernel/head_64.S:405`) | Recognition only at eligible boundaries (contract 49: IF, shadow, priority); HLT parks the vCPU until an event (host wait); POPF of IF/AC/IOPL as a transaction | Eligibility model; QEMU SMP programs | X2 |
| NMI | Stop/panic and backtrace paths; IDT NMI on IST1 (`K kernel/idt.c:86`) | One pending bit + blocking until IRET (M64); sources: LAPIC ICR/LINT1 | Delivery model (blocking); QEMU single-NMI frames | X2, X3 |
| Local APIC, **xAPIC MMIO** | With no interrupt remapping and an unknown hypervisor (`x86_init.hyper.x2apic_available` is a no-op false, `K kernel/x86_init.c:117`) Linux disables x2APIC (`K kernel/apic/apic.c:1820-1836,1781-1800`) and uses MMIO at 0xfee00000; parallel AP bring-up (`x86_init.c:133`, `kernel/smpboot.c:1057-1065`) reads the APIC ID with an MMIO load (`K kernel/head_64.S:266-271`) | Per-vCPU LAPIC in the resident (M69 "Interrupts"): ID, version, TPR/PPR (CR8), EOI, LDR/DFR, SVR, ISR/TMR/IRR, ESR, ICR, LVT timer/LINT0/LINT1/error; register access through M3 | QEMU's APIC via system programs | X3 |
| LAPIC timer (one-shot, periodic; no TSC-deadline) | `LOCAL_TIMER_VECTOR` (`K kernel/idt.c:154`); CPUID.15 presets the period from the 24 MHz crystal (`K kernel/tsc.c:719-727`) and calibration is skipped, requiring an always-running timer (`K kernel/apic/apic.c:797-811`); TSC-deadline not advertised (CPU 13, M61) | Count at 24 MHz ÷ divider from virtual time V (M29, CPU 13), running while halted | Model of counts vs TSC (exact rational); QEMU for register semantics only | X3, L5 |
| IPIs: fixed, NMI, shorthands | Reschedule/call-function (`K kernel/idt.c:135-138`); `text_poke_sync` = `on_each_cpu(do_sync_core)` (`K kernel/alternative.c:2306-2313`); TLB shootdown is guest software over IPIs (H §3) | ICR write → target pending state (atomic) + kick (precise stop, slice-3 §1) | QEMU SMP programs at 2–4 CPUs | X3 |
| AP start: MADT v0 mailbox | `acpi_wakeup_cpu` (`K kernel/acpi/madt_wakeup.c:172-224`); `do_boot_cpu` prefers it over INIT/SIPI (`K kernel/smpboot.c:877-928`); Debian AP runs `trampoline_start64`: LIDT/LGDT, LOCK BTS, MOV SS/DS/ES/FS/GS, MOV CR3, LRETQ (`K realmode/rm/trampoline_64.S:214-248`) | APs wait in the resident; the responder snapshots ID/vector, clears command, enters the vector in long mode, vector page identity-mapped, IF=0 (B 02; M69). **INIT/SIPI: decided deferred** (not reached on this path, `smpboot.c:877-928`; M64 keeps its definition) | Mailbox protocol model (ACPI 6.5 §5.2.12.19, B 02); trampoline transcription vs QEMU entering the same code in long mode | X4 |
| PIC, PIT + port 0x61, RTC, IOAPIC, ACPI PM | `quick_pit_calibrate` runs because leaf 0x16 is not advertised (`K kernel/tsc.c:906-918`; M17); the PIT is then stopped since `apic_needs_pit` is false with CPUID.15 + ARAT + APIC (`apic.c:748-786`, `kernel/i8253.c:32-56`, `kernel/time.c:57-65`) and the IO-APIC timer check returns (`K kernel/apic/io_apic.c:2060-2061`); CMOS wallclock (`x86_init.c:149`); IOAPIC routes COM1 IRQ4 (M23, M60, M69). **PM timer required:** without TSC_ADJUST the TSC keeps `CLOCK_SOURCE_MUST_VERIFY` (`tsc.c:1248-1263,1561`; TSC_RELIABLE only for Goldmont, `tsc.c:716-718`), so the clocksource watchdog needs a second clocksource; with no HPET that is the ACPI PM timer (watchdog selection in `kernel/time/clocksource.c` not re-read) | Register models with exact-once side effects; PM timer, PIT, RTC from the shared V (M29); IRQ → IOAPIC → LAPIC and EOI back (M23 caveat) | Each against QEMU's device model through port/MMIO programs | L4 |
| Idle states | C2/C3 come from `_CST` or FADT P_BLK (P_BLK path: `K drivers/acpi/processor_idle.c:213-246`; `_CST` is parsed elsewhere); without them only C1 = HLT (`processor_idle.c:278-292`) | Tables give no `_CST` and no P_BLK, so only HLT idle is reachable | Table check (L2); R1 idle composite | L2 |

### 1b. CPU state and system instructions

| Feature | Kernel use (cited) | General path must | Check | Chunk |
| --- | --- | --- | --- | --- |
| CPUID | `verify_cpu`: leaves 0, 1, 0x80000000/1; REQUIRED_MASK0 = FPU, PSE, MSR, PAE, CX8, PGE, FXSR, CMOV, SSE, SSE2; MASK1 = LM (`K kernel/verify_cpu.S:52-131`, `include/asm/required-features.h:13-81`); 7.0/7.1/0xD (`K kernel/cpu/common.c:995-1009`); 0x15 (`tsc.c:659-730`); LA57 probe (`K boot/compressed/pgtable_64.c:109-143`); microcode read does CPUID 1 (below) | The CPU 16 table leaf by leaf: 0, 1, 2 (`0x0000ff01`), 4 (four cache levels), 6 (ARAT), 7.0, 0xB (topology), 0xD.0–2 (XCR0 0x7), 0x15 = (3, 125, 24000000) (CPU 13), 0x40000000 (`NatemuNatemu`), 0x80000000–8 (brand, invariant TSC, 40/48 bits); dynamic OSXSAVE and APIC bits; MISC_ENABLE bit 22 limit (CPU 17); a draining entry | Unit test per leaf against the CPU 16 table; `verify_cpu` and LA57 probe transcriptions | S1 |
| MSRs, Linux-reached set | Non-safe: GS_BASE (`K kernel/head_64.S:61-69,365-368`), EFER SCE+NX (`head_64.S:378-396`), MISC_ENABLE (`verify_cpu.S:93-97`, `kernel/cpu/intel.c:233,333,463,576`), UCODE_REV 0x8B written 0, CPUID 1, read (`K include/asm/microcode.h:62-75`, `intel.c:246`; latch per CPU 17), TSC_AUX written because RDTSCP is advertised (`common.c:2213-2214`), STAR/LSTAR/SFMASK (`common.c:2121,2146,2158`), FS_BASE/KERNEL_GS_BASE (`common.c:2341-2342`), PAT (`mm/pat/memtype.c:234,257`), MTRRcap/def-type and, when MTRRdefType.E=1, the variable and fixed ranges through `mtrr_generic_set_state` (`kernel/cpu/mtrr/mtrr.c:584-600`, `kernel/cpu/cacheinfo.c:1115-1129`), TSC, APIC_BASE (`head_64.S:247`). `_safe`: SYSENTER_* (`common.c:2131-2139`), PLATFORM_INFO, MISC_FEATURES_ENABLES, TEST_CTRL (`intel.c:528-549,997`) | Whitelist = CPU 9/16/17 tables; validation before commit; canonical checks (FS/GS/KERNEL_GS/LSTAR); all else **delivered** #GP(0). Initial MTRR state (enabled or not, ranges) is a manifest field (Q3) | `system_fault_model.py` with the rederived profile; QEMU for value formats | S1 |
| CR0: PE, MP, ET, NE, WP, AM, PG; TS/EM; CD/NW | `CR0_STATE` on every CPU (`K kernel/head_64.S:398-401`); TS/EM cleared (`K kernel/fpu/init.c:32-36`); CD set/cleared with WBINVD around MTRR programming when MTRRs are enabled (`cacheinfo.c:1052-1110`) | Accept MP/NE/AM/TS/EM/CD/NW (today only WP, H §3) with effects: TS → #NM; CD/NW no cache model; AM: #AC needs CPL3 + RFLAGS.AC, which stays a typed refusal unless built | `system_fault_model.py`; QEMU | S2a |
| CR4 | PSE/PGE (`head_64.S:212-223`), OSFXSR/OSXMMEXCPT (`fpu/init.c:25-30`), OSXSAVE (`kernel/fpu/xstate.c:173-195`), FSGSBASE (`common.c:2283-2292`), SMEP (`common.c:367-368`), pinning (`common.c:407,494-498`) | Valid per CPU 16: DE, TSD, PSE, PAE, PGE, PCE, OSFXSR, OSXMMEXCPT, FSGSBASE, OSXSAVE, SMEP; reserved: VME, PVI, MCE, UMIP, LA57, VMXE, SMXE, PCIDE, SMAP, PKE and the rest (H §3). The fault model's current CR4 profile (`tools/system_fault_model.py:17-18`, lists UMIP, LA57, VMXE, SMXE, PKE, MCE as valid) must be rederived from CPU 16 by an independent author | Same | S1, S2a |
| CR2, CR8 | #PF handlers read CR2 (`K boot/compressed/ident_map_64.c:363`, `kernel/head64.c:365`); CR8 = TPR | State; CR8 ↔ LAPIC TPR | QEMU | S2a, X3 |
| Debug registers | `initialize_debug_regs` on every CPU (`K kernel/cpu/common.c:2182-2191`) | DR0–7 as state. **TF, RF and enabled breakpoints stay a typed refusal: this amends CPU 16 ("Debug registers, TF/RF … remain required") and needs the owner (Q4)** | QEMU | S2a |
| Descriptor tables and segments: LGDT/LIDT/LTR/SGDT/SIDT/STR, LLDT, MOV Sreg (64-bit), MOV r←Sreg, LRETQ | Decompressor (`K boot/compressed/head_64.S:303-306,368,374,443`), kernel head (`K kernel/head_64.S:71-77,338-353`), trampoline (above), `load_mm_ldt` (`common.c:2362`; its LLDT form to be confirmed); LTR runs with the writable direct GDT (`common.c:719-726,2278`); later loads use the read-only fixmap GDT (`common.c:730-737`, `mm/cpu_entry_area.c:178-199`) | Descriptor fetch through the guest walk; the SDM accessed-bit and TSS busy-bit writes are real writes (through `CodeWrites`, s3g-plan §3): a clear A bit in a read-only GDT page faults as on hardware, so S2b verifies the kernel's descriptors carry A set; flat 64-bit only (D1) | `system_fault_model.py` + `modes_model.py`; QEMU | S2b |
| SWAPGS, SYSCALL/SYSRET, RDGSBASE/WRGSBASE | `entry_SYSCALL_64` (`K entry/entry_64.S:87-166`); paranoid WRGSBASE (`entry_64.S:989,1428`) | Transactions; SYSRET canonical-RCX rule; FS/GS bases in the FEX frame (I7) | QEMU CPL0↔3 | S3 |
| RDTSC/RDTSCP, TSC_AUX | KASLR entropy in the decompressor uses RDTSC (`K lib/kaslr.c:73-80`); TSC clocksource (`tsc.c:694-695`) | `base(V) + offset` per vCPU at 1 GHz (M29, M61, CPU 13); TSC_AUX | Model (monotonic, exact rate) | S3, L5 |
| RDRAND (advertised by CPU 16) | Decompressor KASLR (`K lib/kaslr.c:64-71`, `boot/compressed/kaslr.c:540,808`); boot sanity loop (`K kernel/cpu/rdrand.c:24-47`); kernel random seeding via `arch_get_random_longs` (`K include/asm/archrandom.h:20-50`) | Typed entropy service with the architectural CF result (CPU 16), or removed from the profile (Q3) | Statistical sanity (rdrand.c's own test) + CF semantics vs SDM | S3 |
| VERW | With the CPU 16 identity and no ARCH_CAPABILITIES, Linux sets MDS and Meltdown (`K kernel/cpu/common.c:1415-1419,1533-1540`): `CLEAR_CPU_BUFFERS` = `verw` on every return to user (`K include/asm/nospec-branch.h:379-393`, `kernel/cpu/bugs.c:268-284`) and PTI switches CR3 at each kernel entry and exit (`K mm/pti.c` `pti_check_boottime_disable`) | VERW: descriptor check, ZF (shares LSL's helper); CR3 switches work today (full flush per load, H §4) | SDM model; QEMU | S3 |
| XSETBV/XGETBV, XSAVE/XRSTOR, FXSAVE/FXRSTOR, LDMXCSR/STMXCSR, FNINIT | `fninit` + `fxsave` (`K kernel/fpu/init.c:44,107`); XSAVE path when advertised (`fpu/xstate.c:173-195,316-326,748-764`); XRSTOR and user-buffer XSAVE carry exception-table fixups (`K kernel/fpu/xstate.h:124-128,153,163-167`) | Image formats (CPU 16: 512 + 64 header + YMM-high 256; XCR0 ⊆ 0x7); XRSTOR #GP on a bad header, #PF on user buffers, both delivered | FEX ASM tests; SDM model for the faults | I5b |
| WBINVD/INVD, CLFLUSH, PAUSE, HLT | WBINVD in `cache_disable` (`cacheinfo.c:1076,1093`) | No cache model (coherent WB backing): drain and order like a fence | QEMU (state unchanged) | S3 |
| Kernel self-modifying code | Alternatives at boot (`K kernel/alternative.c:583,639,1979`); jump labels and static calls via INT3 + `text_poke_bp_batch` (`alternative.c:2462-2482`); `__text_poke` writes through the `poking_mm` alias under a temporary CR3 (`alternative.c:2022,2086-2160`) then `text_poke_sync` IPIs | Cross-vCPU code coherence by physical page (slice-3 S3d) through any alias and CR3; CR3 loads flush (cost) | Probe: patch code through a second mapping under another CR3 while peers execute it behind an INT3, then IPI sync (QEMU reference) | R1a, R1b |

### 1c. Instruction coverage (FEX + admission + recovery)

The census C0 (§3) lists the forms the real binaries contain; rows below are the minimum known from source.

| Feature | Use (cited) | General path must | Check | Chunk |
| --- | --- | --- | --- | --- |
| Small integer forms: CLD/STD/CLC/STC/CMC, LAHF/SAHF, SHLD/SHRD, CMPXCHG/XADD (non-locked), ENTER/LEAVE, PUSHF, prefetch hints, POPCNT/LZCNT/TZCNT/BMI per profile | `cld` at every entry (`K boot/compressed/head_64.S:298`); `std; rep movsq; cld` (`:431-433`); `pushf` (`K kernel/verify_cpu.S:35-37`); refusals measured in H §2 | Admit in `fexc_slice1.inc`; PUSHF reads IF/IOPL/AC from the bank; POPF stays a transaction | FEX ASM suite; QEMU corpora; memory-form fault cases | I1 |
| FS/GS overrides (64-bit) | All kernel C uses `%gs` per-CPU data and the stack canary at `%gs:40` (`K kernel/head_64.S:355-368`); TLS via `%fs` | Frame fields for FS/GS base, admission of their frame loads, site address = base + EA with the canonical check on the final address (guard patch 0007 stays, P2 §2); SWAPGS/WRMSR/WRGSBASE update the field | QEMU corpora with `%fs:`/`%gs:`; fault cases on base + EA | I7 |
| REP MOVS/STOS/LODS/CMPS/SCAS, both DF | Decompressor `rep movsq`/`rep stosq` (`boot/compressed/head_64.S:432,463`); `clear_page` (`K lib/clear_page_64.S:20`); `rep movsb` in uaccess before alternatives (`K include/asm/uaccess_64.h:128-129`); `rep movsq` copy without ERMS (`K lib/copy_user_64.S:71,98`; ERMS not advertised, CPU 16) | Contract 44: RCX/RSI/RDI per element, RIP at the REP, precise mid-string faults; interrupt windows between iterations allowed (SDM Vol. 1; contracts §3 item 9) | QEMU corpora; element-boundary fault matrix vs extended `run_slice1_faults.Machine` | I2 |
| LOCK forms incl. XADD, CMPXCHG, CMPXCHG8B/16B, BTS/BTR/BTC | Spinlocks and atomics; trampoline LOCK BTS (`K realmode/rm/trampoline_64.S:233`) | FEX LSE words only at described atomic sites; nothing committed at an abort; 16-byte-crossing → `unsupported_atomic` (slice-3 §2) unless shown atomic on the M4 | `litmus` RMW cells (`ordering_model.py` extended); chains; fault cases | I3 |
| SSE/SSE2 (required, `required-features.h:62-64`), SSE3–4.2 per CPU 16 | FPU init; every userspace program (H §2) | XMM0–15 + MXCSR in the frame; 16-byte access sites in fexc, admission, recovery; #NM (CR0.TS), #UD without OSFXSR, #XM/#UD per OSXMMEXCPT; vector ordering cells (slice-3 §2) | FEX ASM SSE tests (480 refused today); QEMU corpora; vector fault matrix; litmus vector cells | I5a, I5b |
| AVX/AVX2, FMA, F16C (CPU 16 advertises them) | glibc chooses AVX2 paths when CPUID and XCR0 allow | XCR0 bit 2 and full YMM state required while advertised | FEX ASM VEX tests (726) | I8 (Q3) |
| x87, reduced precision | `fninit` (`fpu/init.c:44`); userspace `long double` | FEX `X87ReducedPrecision` (`third_party/FEX/FEXCore/Source/Interface/Config/Config.json.in:621-627`); FXSAVE images in 80-bit format; inventory reduced-mode ops that still call FEX helpers (run in the resident or refuse) | FEX `unittests/ASM/X87_F64` (150, `FEX_X87REDUCEDPRECISION=1`); QEMU for exact-by-construction values | I6 |
| NOPL, ENDBR64 | Alternatives padding | Already admitted (`tools/fexc/shim/fexc_slice1.inc:92,124-130`) | — | — |

### 1d. Memory

| Feature | Use | General path must | Check | Chunk |
| --- | --- | --- | --- | --- |
| Guest RAM and layout | **Decided:** M69 map, default L = 2 GiB (configurable 1–3 GiB), H = 0; tests choose their own size | `GUEST_RAM_BYTES` (4 MiB, `crates/abi/src/execution.rs:61`) and all per-page structures from the manifest; holes are non-RAM; shadow pool (`POOL_PAGES` = 32, `crates/abi/src/shadow.rs:20`) sized for a kernel working set, resets counted | Standing suite at 4 MiB and 2 GiB; programs above 16 MiB and at holes | M1 |
| Memory types: PAT, PWT/PCD, MTRR | Linux PAT = WB, WC, UC-, UC, WB, WP, UC-, WT (`K mm/pat/memtype.c:344`, written `:234`); LAPIC/IOAPIC mapped UC | Walker evaluates PAT/PWT/PCD (and MTRR if advertised) instead of halting (`shadow status=1 detail=7`, slice-3 S3a2); RAM of any type runs as coherent WB (declared deviation); device pages take M3 | Paging cells with PAT bits (`coherence_model.py` profile updated); QEMU | M2 |
| MMIO from translated code | xAPIC, IOAPIC | Device ranges never shadowed; an abort at a described load/store site completes once through the device owner with the site's width, direction and register, then resumes after that native access; string/vector/atomic MMIO → typed stop | QEMU programs on device pages; #PF-before-device priority | M3 |
| 1 GiB pages | Only with GBPAGES | Not advertised (CPU 16); PS in a PDPTE → #PF(RSVD) (contract 10) | Existing cells | — |

### 1e. Devices, firmware interface, loader

| Feature | Use | General path must | Check | Chunk |
| --- | --- | --- | --- | --- |
| Loader | 64-bit boot protocol (M69 recipe; CLM-03-03-01; `Documentation/arch/x86/boot.rst` v6.12) | Validate header, zero page with setup header, E820, cmdline, initrd, `acpi_rsdp_addr`; aligned K ≥ 16 MiB reserving `init_size`; identity map; BSP state; host entry point outside the test driver (slice-3 phase-end deviation) | Independent boot.rst checker; malformed-header tests; the Debian image header (M13) | L0, L1 |
| ACPI tables | MADT (LAPICs, IOAPIC, IRQ0→GSI2, mailbox 0x10), FADT (PM block, SCI, no S3/S4, no C2/C3), FACS, DSDT (PNP0501, `_S5`), RSDP via `acpi_rsdp_addr` (M69); Linux writes the mailbox entry (M69) | **Default decided by M24/M69: QEMU's conventional builder and linker, finalized by the loader** (deviation only on the owner's initiative); one description for APIC IDs, N, mailbox | `iasl -d` round trip; ACPI 6.5 layouts | L2 |
| Port I/O | UART, PIT/0x61, PIC, RTC, PM, port 0x80 | IN/OUT/INS/OUTS exact-once to the host device owner; CPL3 via IOPL and TSS I/O bitmap. **Unassigned ports: a manifest rule — reads return all-ones, writes are ignored** (QEMU is not a reference here) | Assigned ports vs QEMU; unassigned vs the rule | L3 |
| Serial 16550A at COM1 | earlycon/console (M69); IRQ4 edge/high (M60) | 16550A model (FIFOs, LSR/IIR, scratch for 8250 autoconfig), IRQ4 line | Same sequences under QEMU's UART | L3 |
| Legacy island | See §1a | **Default decided by M20/M69: QEMU's PIC/PIT/0x61/RTC/IOAPIC/PIIX4 PM in the host** (Q6 only if deviating) | QEMU device models | L4a, L4b |
| Virtual time | TSC, LAPIC, PIT/RTC/PM from one V (M29, M61) | Host clock service; host wake-ups only prompt rechecks | Exact-rational unit tests | L5 |
| Shutdown | `_S5` PM1 write (M22) | Typed VM stop | QEMU port sequence | L4b |

## 2. Dependencies and order

**Privilege level (P2, done).** The study recommends staying at EL1: moving to EL0 with exact x86 semantics removes no
generated-code word, the address guard stays because it gives exact non-canonical #GP/#SS, and dispatch, faults and
linked edges cost the same at both levels (P2 "Answer in brief", §5). Nothing in §1 therefore depends on the privilege
level; the plan proceeds at EL1. P2's options A (EL0 with guards kept, defence in depth) and B (`ldtr`/`sttr` guest
accesses from EL1, removing the guard for scalar accesses) are owner items for the deferred list (Q5).

**Remaining design choice (X2/L5):** how a pending interrupt reaches a vCPU running linked code — the existing precise
stop (host kick via `hv_vm_protect`, slice-3 §1) or an in-VM native timer for local LAPIC deadlines. X2 builds on the
precise stop; L5 measures whether timer kicks need the second route.

**Decision point P1 (performance diagnosis, reported; independent replication pending; `experiments/track03/perf-diagnosis/`).**
The owner decides which measured costs, if any, enter this phase; the performance rule holds otherwise. Placed before
the chunks most exposed to those costs (S3 syscall/PTI CR3 switches, X3 IPIs); no result is presumed.

```
C0 (needs Q2) ────────────────────────────── feeds I1–I8
ISA:        I1 → I7 → I2 → I3 → I5a → I5b → I6, (I8)
System:     S1 → S2a → S2b → S3 (needs I7)
Delivery:   X1a (needs S2b) → X1b (needs S3 for LSL) → X1c
Events:     X2 (driver-injected events; needs X1a) → X3 (needs M3, L5) → X4 (needs I3, I7)
Memory:     M1 → M2 → M3
Devices:    L0 → L1, L2;  L3 → L4a → L4b;  L5
P1 decision ─ before S3 and X3
End:        R1a → R1b → R2
```

## 3. Chunks

One agent each, about one run unless marked; an independent reviewer at each checkpoint (AGENTS.md). **Standing
suite** = the [boot-handoff §1](boot-handoff.md) command block. Each chunk runs its focused subset during work and the
full standing suite at its end; 64-bit objects of the existing corpora stay byte-identical unless the chunk changes code
generation and says why. **Stop:** at the bound, report what passes and each unsupported capability with its reason.

**Fault references (every fault-related check uses these).** QEMU is wrong for some faults: pinned QEMU 11.1.2 accepts
or hangs on several invalid control writes and unknown MSRs (`tools/system_fault_model.py:9-12`), and differs on #PF
error bits in some RMW and never-mapped cases (`docs/restructure/slice2-contract.md:172`).

| Fault class | Vector, error code, CR2 | Frame contents and IRET mechanics |
| --- | --- | --- |
| #PF (paging cells, access forms) | `run_slice1_faults.Machine` SDM model | QEMU, after agreeing with the delivery model on a sample |
| #GP/#UD from system instructions (CR/MSR/descriptor/LTR/LSL/VERW/IRET checks) | `system_fault_model.py` with the CPU 16 profile rederived by an independent author; `modes_model.py` where it applies | QEMU as above |
| #SS/#GP non-canonical, #DE | Existing SDM probes and division probes | QEMU as above |
| #NM, #XM, #UD for SSE/x87 gating; XRSTOR header #GP | New SDM table in the delivery model | QEMU as above |
| #DF classes, faults during delivery, triple fault, NMI blocking | Delivery model only | Delivery model only |

| Chunk | Content | Check (reference) | Keep green (focus) | Stop when |
| --- | --- | --- | --- | --- |
| **C0** census (after Q2) | Static census of instruction forms admission refuses, over the Debian decompressor, the decompressed vmlinux and the initramfs `/init` binaries, by H §2's static-share method with fexc's classifier | Counts per form and per binary; spot-check against `llvm-objdump` | — | every refused form is listed with its I-chunk or a proposed typed refusal |
| **L0** host split | Host library + driver crate; generic stop-ELR hook for `forge_stop` (slice-3 phase-end deviation); manifest-taking boot entry stub | Standing suite; `contain` tampers fire | all | green; no tamper code in the library |
| **S1** CPU profile | CPUID table and MSR whitelist keyed to the CPU 9/16/17 tables plus a grep of every non-safe `rdmsr`/`wrmsr` in the fetched sources (and C0 when available); UCODE_REV latch, TSC_AUX, MTRR state from the manifest; CPUID/RDMSR/WRMSR transactions; rederived fault-model profile (independent author) | Unit tests per leaf and per MSR against the record tables; `verify_cpu` and LA57 transcriptions; unknown MSR → #GP | `system` | every leaf of the CPU 16 table and every non-safe MSR found by the grep has a test |
| **I1** small integer forms | §1c row 1 | ASM suite pass count rises, 0 fail; QEMU corpora at block sizes default/1; memory-form fault cases | fixtures, seed, faults, ASM | each form admitted or its refusal explained |
| **I7** FS/GS overrides | §1c FS/GS row | QEMU corpora with `%fs:`/`%gs:` at CPL0/3; fault cases on base + EA | fixtures, faults | per-CPU `%gs:` load/store/RMW programs match |
| **M1** RAM and layout | M69 map, per-page structures, pool sizing | Standing suite at 4 MiB and 2 GiB; corpora above 16 MiB; pool resets reported | all | green at both sizes |
| **L1** loader | §1e loader | Independent boot.rst checker; malformed-header tests; Debian header (Q2) | unit | checker passes; every rejection tested |
| **L2** ACPI tables | M24/M69 tables, no `_CST`/P_BLK | `iasl -d` clean; MADT ↔ CPUID ↔ mailbox | unit | tables cross-agree for N = 1..4 |
| **S2a** control registers (~1 run) | CR0 bits, CR4 per profile, CR2, CR8 store, DR0–7, EFER.SCE | `system_fault_model.py` (rederived); QEMU; head sequence `head_64.S:378-405` transcribed | `system` | cited sequences match |
| **S2b** descriptors and segments (~1 run) | LGDT/LIDT/LTR/SGDT/SIDT/STR/LLDT, MOV Sreg, MOV r←Sreg, LRETQ; descriptor fetch helper with A/busy writes | Same; decompressor and head GDT/segment sequences; read-only GDT case | `system`, `coherence` | cited sequences match; A-bit write on a read-only page faults per SDM |
| **X1a** same-CPL delivery + IRET + delivery model | IDT walk, gates, error codes, CR2, IRETQ CPL0→CPL0, INT3/INT n/INTO/UD2; the delivery model (independent author). **Old corpora:** the driver supplies a default logging IDT whose handlers record vector, error, CR2 and frame and then `hlt`; programs with no IDT get it, and the fault runners are migrated explicitly to compare that log (references above) instead of a stop | Migrated slice-1/2 fault corpora; decompressor-style on-demand #PF program; `int3_selftest` transcription | faults, loop faults, `system`, stack audit | all migrated corpora pass; model and QEMU agree on the sample |
| **S3** syscall, time, misc system | SWAPGS, SYSCALL/SYSRET, RDGSBASE/WRGSBASE, RDTSC/RDTSCP/TSC_AUX, RDRAND (if kept), **LSL**, VERW, WBINVD/INVD/CLFLUSH, CPUID/IRET draining | QEMU CPL0↔3 round trips with SWAPGS and `%gs`; LSL/VERW vs SDM model; TSC model; RDRAND per §1b | `system`, `smp` | round trips match at N = 1..4 |
| **X1b** CPL3 stack switch + IST | TSS.RSP0, IST1–7, IRETQ to CPL3 | QEMU frames; delivery model; paranoid-entry-shaped NMI/#DF/#DB programs using `lsl` (`calling.h:401-420`) | faults, `system` | CPL3 faults and IST vectors match |
| **X1c** #DF, nesting, refusals → exceptions | #DF classes, faults during delivery, triple fault → typed stop; refused-but-defined cases (unknown MSR, reserved bits) delivered | Delivery model only for #DF/nesting; QEMU frames otherwise | faults, `system` | model cases pass; no defined exception remains a stop |
| **M2** memory types | PAT/PWT/PCD/MTRR in the walker | Paging cells with PAT bits (model updated, independent author); QEMU | faults, `sharedpt` | no `detail=7` halt from a valid PAT setup |
| **M3** MMIO completion | Device ranges, site-based completion, typed stops | QEMU programs on a test device page; fault priority | faults, `contain` | 1–8-byte device accesses complete exactly once |
| **X2** eligibility, HLT, NMI | IF, STI/MOV SS shadows, POPF, pending events, priority, HLT park/wake, NMI pending/blocking (M64). **Event source:** a driver hook posts a vector or NMI into the vCPU's real pending-event state and kicks it (instrumentation controls a real site; vm-restructure "Correctness"), so X2 does not wait for X3's ICR | Eligibility model; QEMU programs whose interrupts come from QEMU's own sources at matching points | `kicks`, `contain` progress | contract 49 has a general-path check |
| **L5** clock service | V, TSC, 24 MHz LAPIC rationals; host wake-ups; measure kick latency for timer expiry (§2 design choice) | Exact-conversion unit tests; cross-vCPU monotonicity | unit | rates and epochs exact |
| **X3** LAPIC and IPIs | §1a LAPIC rows on M3; ICR → pending + kick; timer on L5 | QEMU APIC register programs; IPIs at 2–4 vCPUs; timer model | `smp`, `kicks` | Linux-used registers and IPIs match |
| **X4** mailbox AP start | Resident waiting state, responder, N = 1..4 | Mailbox model; trampoline + `secondary_startup_64` prefix transcription vs QEMU | `smp` | APs reach their stacks with the lock released |
| **L3** port I/O and UART | IN/OUT/INS/OUTS, IOPL/bitmap, unassigned-port rule, 16550A, IRQ4 line | QEMU for assigned ports; the manifest rule for unassigned | `system`, `contain` | 8250 sequences give QEMU's results |
| **L4a** IOAPIC + PIC (~1 run) | QEMU models in the host; IRQ routing to X3; EOI back | QEMU device models; IRQ4 → IOAPIC → LAPIC once per edge | `smp` | routing and EOI match |
| **L4b** PIT/0x61, RTC, PM timer/PM1, S5 (~1 run) | From V; quick-PIT-calibrate and PIT-disable sequences; PM timer as watchdog source | QEMU device models; PM/TSC rate agreement | `smp`, `contain` | cited Linux sequences produce QEMU's values |
| **I2** REP strings | §1c REP row | QEMU corpora; element-boundary fault matrix; interrupt windows (after X2) | faults, ASM REP/STOS | contract 44 has a general-path check |
| **I3** LOCK forms | §1c LOCK row | `litmus` RMW cells (model extended, independent author); chains; faults | `litmus`, faults | ASM `Atomics` pass or refused with reason |
| **I5a** SSE state and arithmetic (~1–2 runs) | XMM/MXCSR in the frame, vector sites, recovery, ordering cells | FEX ASM SSE; QEMU corpora; vector fault matrix; litmus vector cells | ASM, `litmus`, faults | SSE ASM tests pass or refused with reason, 0 fail |
| **I5b** FXSAVE/XSAVE family and gating (~1 run) | FXSAVE/FXRSTOR, XSAVE/XRSTOR/XSETBV/XGETBV, CR0.TS/CR4 gating; XRSTOR #GP on a bad header, #PF on user buffers via the exception table; XCR0 bit 2 and YMM state while AVX is advertised | FEX ASM tests; SDM table for the faults | ASM, faults | images match CPU 16 layout |
| **I6** x87 reduced precision | FEX reduced mode; 80-bit images; helper inventory | `X87_F64`; QEMU exact-by-construction | ASM | 0 fail; deviation recorded |
| **I8** AVX/AVX2 (if Q3 keeps them) | YMM ops | FEX ASM VEX tests | ASM | as I5a |
| **R1a** single-vCPU composites (~1 run) | Decompressor entry; kernel head (`head_64.S:37-405`); early #PF table building; `int3_selftest`; alternatives/static-call patching through a second mapping under another CR3; syscall round trip with PTI CR3 switch and VERW; timer tick into `sti; hlt`; serial write with IRQ4 | QEMU system mode with the same programs | all | each matches at block sizes default/1 |
| **R1b** SMP composites (~1 run) | Mailbox AP start into the head; `text_poke_bp` + IPI `sync_core`; paranoid NMI with `lsl`; IPIs under load | QEMU at N = 2..4 | all | each matches at N = 2..4 |
| **R2** phase-end audit | Contract inventory (44, 49, 51, SSE parts of 1/2/5, LOCK parts of 43); stack audit; integration handoff page | Independent review of the whole path | all | §4 holds |

## 4. Phase end: ready for boot integration

- [ ] P2's recommendation and P1's outcome recorded by the owner; the deferred list holds options A/B if chosen (Q5).
- [ ] Every §1 row maps to an R1 composite or a named check, and every refused form in the C0 census is implemented or
      an accepted typed refusal.
- [ ] CPUID/MSR profile as recorded (CPU 9/16/17 or the owner's variant), unit-tested leaf by leaf and MSR by MSR;
      fault-model profile rederived independently.
- [ ] Exceptions and interrupts delivered, not stopped: migrated fault corpora pass through the logging IDT; contracts
      44 and 49 have general-path checks in [contracts.md](contracts.md).
- [ ] LAPIC (xAPIC MMIO), timer, IPIs and the mailbox AP start work at N = 1..4.
- [ ] Loader output passes the independent boot.rst checker for the chosen image; ACPI tables disassemble cleanly and
      agree with CPUID/MADT/mailbox.
- [ ] UART, port I/O (incl. the unassigned-port rule), PIC/IOAPIC, PIT, RTC and PM timer match QEMU on the cited
      sequences; PM timer and TSC agree in rate.
- [ ] Guest RAM at 2 GiB with the M69 holes; standing suite green at 4 MiB and 2 GiB.
- [ ] ISA: ASM suite 0 fail with SSE, XSAVE, x87 (`X87_F64`), REP, LOCK and flag-move tests passing or refused with
      reason; AVX per Q3.
- [ ] R1a/R1b match QEMU at N = 1..4, block sizes default and 1.
- [ ] Standing suite green; `check_resident_stack.py` passes on the final binary; nothing staged.
- [ ] Integration handoff page: boot command, manifest inputs, known limits and costs (incl. PTI CR3 flushes).

## 5. Owner questions and recorded decisions

**Owner decisions (October 28):** kernel = Debian 6.12 (32-bit stays deferred); fetching the Debian kernel,
config and initramfs into natemu-raw is allowed (nothing committed); mitigations are booted off on the command line
(`mitigations=off`), while VERW and CR3 switches are still implemented correctly; the accepted CPU 16 profile is kept
(AVX/AVX2/FMA via FEX AVX128, RDRAND, MTRR in this phase). The owner asked why a CR3 load discards the code cache:
it is an S3c1 simplification, not an x86 requirement — see the code-cache-across-CR3 item added to the plan.
Also: single-step (TF/RF) and hardware breakpoints are built in this phase as CPU 16 requires (no deferral); the
loader may run Debian's real decompressor up to its jump into the decompressed kernel as a smoke test.

**Decided from evidence (recorded, not questions):** guest RAM default 2 GiB, tests choose their own size (M69);
INIT/SIPI deferred, unused with the mailbox (`K kernel/smpboot.c:877-928`; M64 keeps the definition); legacy devices
and ACPI tables reuse QEMU's (M20, M24, M69); privilege level stays EL1 (P2; the owner confirms it once).

1. **Kernel.** Debian 6.12.107 (assumed; no 32-bit code) or Ubuntu 6.8 (its AP trampoline needs compatibility mode,
   paging off, IA-32e activation, far transfers and 32-bit LOCK BTS: s3g-plan option (b), ~2 runs more)?
2. **Fetching inputs.** May the phase fetch the Debian kernel (12 MB, M13 digest), its config (headers package, B 08)
   and a matching initramfs for C0 and L1? No image bytes are retained locally (s3g-plan §1).
3. **CPU profile.** Keep CPU 16 as accepted (AVX/AVX2/FMA/F16C → I8 in this phase; RDRAND → typed entropy service;
   MTRR → the CR0.CD/WBINVD/MTRR-range sequence) or amend it, e.g. through SSE4.2 + XSAVE with XCR0 = 3, no
   AVX/BMI/RDRAND? Also the manifest's initial MTRR state (enabled with ranges, or disabled). Linux and glibc follow
   whatever is advertised.
4. **TF and breakpoints.** Deferring TF/RF single-step and enabled hardware breakpoints amends CPU 16 ("Debug
   registers, TF/RF … remain required"). Accept the deferral as a typed refusal for this phase?
5. **Deferred optimisation list.** Record P2's option B (`ldtr`/`sttr` guest accesses from EL1, removing the address
   guard for scalar accesses; +4.5% on one serial loop) and option A (EL0 with guards, defence in depth) there?
6. **Deviate from the recorded device choices?** Only if the owner wants minimal Rust models or a Rust ACPI builder
   instead of QEMU reuse (M20, M24, M69).
7. **Mitigations.** With the Haswell identity Linux enables PTI (a CR3 load on every kernel entry and exit, and each
   CR3 load currently flushes the vCPU's whole code cache and shadows, H §4) and VERW on every user return. Accept the
   cost, pass `mitigations=off`/`pti=off` on the command line (no kernel change), or advertise ARCH_CAPABILITIES
   "not vulnerable" bits (a claim about the emulator's speculation)?
8. **Loader smoke test.** May L1 run the decompressor up to its jump into the decompressed kernel, or is that
   integration?
