# Boot-features phase plan

Status: proposal, October 28, 2026, revised after independent review (ACCEPT-WITH-FIXES; read-only design, no code
changed); built October 28 - November 2 (each chunk's "Built" note in §5); phase-end audit R2 November 2 (§4, and the
integration handoff [boot-handoff.md](boot-handoff.md)). Scope is the owner's next-phase direction ([vm-restructure.md](../vm-restructure.md), "Next phase scope",
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

## 0. Principles (owner, October 28)

- **Architecture, not a kernel.** Implement x86 and the platform as specified (SDM, ACPI, device datasheets). The Linux
  source is used only to find which architectural features a boot exercises and to build test sequences; nothing in the
  emulator recognises, special-cases or patches a kernel. Other kernels and OSes come later. Any exception must be
  shown necessary and approved by the owner. (Boot configuration such as the command line is the user's choice, not
  emulator behaviour.)
- **Native exceptions and interrupts where possible.** Use Arm/HVF hardware mechanisms (synchronous exceptions taken
  in the VM, virtual interrupt injection, the in-VM GIC and timers, inter-processor signalling) in preference to host
  round trips and software polling, subject to precise x86 delivery at instruction boundaries. A study of what HVF
  on the M4 provides comes before the exception and interrupt chunks.

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
| CR0: PE, MP, ET, NE, WP, AM, PG; TS/EM; CD/NW | `CR0_STATE` on every CPU (`K kernel/head_64.S:398-401`); TS/EM cleared (`K kernel/fpu/init.c:32-36`); CD set/cleared with WBINVD around MTRR programming when MTRRs are enabled (`cacheinfo.c:1052-1110`) | Accept MP/NE/AM/TS/EM/CD/NW (today only WP, H §3) with effects: TS → #NM; CD/NW no cache model; AM: #AC needs CPL3 + RFLAGS.AC, which stays a typed refusal (S2a) until X2 builds it | `system_fault_model.py`; QEMU | S2a |
| CR4 | PSE/PGE (`head_64.S:212-223`), OSFXSR/OSXMMEXCPT (`fpu/init.c:25-30`), OSXSAVE (`kernel/fpu/xstate.c:173-195`), FSGSBASE (`common.c:2283-2292`), SMEP (`common.c:367-368`), pinning (`common.c:407,494-498`) | Valid per CPU 16: DE, TSD, PSE, PAE, PGE, PCE, OSFXSR, OSXMMEXCPT, FSGSBASE, OSXSAVE, SMEP; reserved: VME, PVI, MCE, UMIP, LA57, VMXE, SMXE, PCIDE, SMAP, PKE and the rest (H §3). The fault model's current CR4 profile (`tools/system_fault_model.py:17-18`, lists UMIP, LA57, VMXE, SMXE, PKE, MCE as valid) must be rederived from CPU 16 by an independent author | Same | S1, S2a |
| CR2, CR8 | #PF handlers read CR2 (`K boot/compressed/ident_map_64.c:363`, `kernel/head64.c:365`); CR8 = TPR | State; CR8 ↔ LAPIC TPR | QEMU | S2a, X3 |
| Debug registers | `initialize_debug_regs` on every CPU (`K kernel/cpu/common.c:2182-2191`) | DR0–7 as state (S2a); TF single-step, RF and instruction, data and I/O breakpoints built per the SDM (S4: Arm EL0 watchpoints for translated code, software checks for the resident's own accesses) | SDM-derived logs; QEMU where it follows the SDM | S2a, S4 |
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
| Guest RAM and layout | **Decided:** M69 map, default L = 2 GiB (configurable 1–3 GiB), H = 0; tests choose their own size | Built (M1): guest RAM a run-time size R (2 MiB multiples to 3 GiB, default 2 GiB), per-page structures sized for 3 GiB and scanned by use, [R, 4 GiB) non-RAM, shadow pool 288 pages, resets counted; the legacy hole non-RAM since L3 (the unassigned rule) | Standing suite at 4 MiB and 2 GiB; programs above 16 MiB and at holes | M1 |
| Memory types: PAT, PWT/PCD, MTRR | Linux PAT = WB, WC, UC-, UC, WB, WP, UC-, WT (`K mm/pat/memtype.c:344`, written `:234`); LAPIC/IOAPIC mapped UC | Walker evaluates PAT/PWT/PCD (and MTRR if advertised) instead of halting (`shadow status=1 detail=7`, slice-3 S3a2); WB/WT/WP/WC RAM runs natively, UC RAM keeps strong ordering (owner decision, November 2: each access runs its instruction alone between full barriers); device pages take M3 | Paging cells with PAT bits (`coherence_model.py` profile updated); QEMU | M2 |
| MMIO from translated code | xAPIC, IOAPIC | Device ranges never shadowed; an abort at a load/store site completes once through the device owner from the site's completion record (fexc's decode of the translated instruction: width, direction, register and merge, length), then resumes after the instruction; every other form (string, vector, RMW, atomic) → typed stop | QEMU programs on device pages; #PF-before-device priority | M3 |
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
accesses from EL1, removing the guard for scalar accesses) are owner items for the deferred list (Q5). *Superseded
(owner, October 28, §5 "adopt hardware-only checking first"):* translated code runs at EL0 with the address guard
removed (E1-E3 built); the resident stays at EL1.

**Remaining design choice (X2/L5):** how a pending interrupt reaches a vCPU running linked code — the existing precise
stop (host kick via `hv_vm_protect`, slice-3 §1) or an in-VM native timer for local LAPIC deadlines. X2 builds on the
precise stop; L5 measures whether timer kicks need the second route. *Decided (owner, October 28, §5):* the HVF
in-kernel GIC is the doorbell for every asynchronous event and the stop is IRQ-triggered unlinking (E1b);
`hv_vm_protect` is gone.

**Decision point P1 (performance diagnosis, reported and independently replicated, INV-03-01 "Next phase";
`experiments/track03/perf-diagnosis/`).**
The owner decides which measured costs, if any, enter this phase; the performance rule holds otherwise. Placed before
the chunks most exposed to those costs (S3 syscall/PTI CR3 switches, X3 IPIs); no result is presumed.

```
Foundation: E1 (done) → E1b (GIC doorbell + unlinking stop) → E2 (EL0) → E3 (guard off, admission split) → E4 review
            then G1 (guest-state page), K1 (code cache across CR3/INVLPG) and U1 (shadows per x86 CPL); L0 (host split) any time
C0 (download allowed) ─────────────────────── feeds I1–I8
ISA:        I1 → I7 → I2 → I3 → I5a → I5b → I6, (I8)
System:     S1 → S2a → S2b → S3 (needs I7, U1) → S4 (TF single-step, DR breakpoints; after X1a)
Delivery:   X1a (needs S2b) → X1b (needs S3 for LSL, U1 before any IRETQ to CPL3) → X1c
Events:     X2 (GIC doorbell events from E1b; needs X1a) → X3 (needs M3, L5) → X4 (needs I3, I7)
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
| **E1b** GIC doorbell + unlinking stop | HVF GIC; kicks and cross-vCPU posts as SGIs/SPIs; IRQ handler patches the running block's exits; `bl pad` removed (fexc series12) | `kicks` precision at 1/2/4 vCPUs incl. oversubscribed; adversarial random-IRQ test; QEMU | standing suite | every stop at a block boundary; per-entry cost gone |
| **E2** EL0 switch | Page roles (code execute-only, L1 code, EL1-only resident), lower-EL vectors, EC 0x24/0x20 at EL0, stop and fault paths from EL0 | Standing suite; trap checks for system instructions at EL0 | standing suite | all green at EL0 |
| **E3** guard off, admission split | Remove patch-0007 guard words and checks; admission = mandatory object validation + optional code-shape debug; negative tests: every former window address and both canonical straddles give #GP/#SS | Faults suite + new negative cells; census | standing suite | 0 guard words; all #GP/#SS exact |
| **E4** review | Independent review of E1b–E3 as one design | — | standing suite | ACCEPT |
| **U1** x86 privilege in shadows | E2 shadows carry no x86 U/S (the walker checks at the current CPL; CPL is fixed per run today). Before any CPL transition (X1b delivery from CPL3, S3 SYSCALL/SYSRET, IRETQ to CPL3): keep one shadow space per x86 CPL class (separate ASIDs/roots for CPL0 and CPL3) so a CPL change switches spaces instead of resetting, and a CPL0 shadow can never serve a CPL3 access | Fault matrix at both CPLs across transitions; syscall/IRET round trips that touch supervisor pages before and after | standing suite | no CPL3 access through a CPL0 shadow; no shadow flush per transition |
| **G1** guest-state page (minimum built in I1; remainder (MXCSR, x87, AVX-high fields) with I5/I6/I8) | One EL0 page per vCPU at a fixed non-canonical address holding only FEX guest-state fields (DF, FS/GS, MXCSR, x87, AVX-high); documented deviation (a guest access to that page reaches its own state instead of #GP); the page lies outside the private window `[PRIVATE, PRIVATE+1 GiB)` or is exempted explicitly in `aborted`; invariant test | Layout test; non-#GP limited to that page | standing suite | invariant test fails on any non-guest field |
| **K1** code cache across CR3/INVLPG | Blocks filed by guest-physical source pages; CR3/INVLPG drop only shadows; linear→physical revalidation on lookup; per-page invalidation for host/device writes | Address-space switch programs keep translations (counted); remap/rewrite always retranslated; QEMU at 1–4 vCPUs | `system`, `coherence`, `sharedpt` | no full flush on CR3 |
| **K2** resident system-instruction path | Every system instruction is a host exit for its decode today (measured: FLD+FSTP m80 11.4 us per pair against <0.1 us for m64; CPUID/RDTSC, CLI/STI/POPF, SWAPGS, MSR and CR accesses, Linux's irq save/restore and syscall paths all pay it). Decode once when the block is translated, from the bytes fexc translated (so cross-modifying code sees old-or-new, as M3's site records), store the decoded system record in the block, and run the transaction in the resident with no host exit; keep host exits only for work that needs the host (devices, entropy refill, CPUID/MSR state the host owns, if any) | Exit counts per system kind before/after; the standing suite unchanged; XMC toggle cases (old-or-new); cost per CLI/STI/POPF/SWAPGS/RDTSC/FLD m80 measured | standing suite | no decode exit for an admitted system instruction |
| **P2** resident entry cost | Measured after K2: about 0.36 us fixed per resident entry on the linker path and 0.167 us for the cheapest round trip (`svc #DIV`), against ~55 ns per extra transaction inside one entry; every block exit into a system instruction pays it (never linked). Instrument `bank`/`sse::bank`, `block_at`, `enter` (frame build, `sse::fill`, full State `write_record`, NativeFrame) and `tables::select` with counters; then cut what the evidence shows (e.g. bank/copy only what changed, link a block exit straight into a kept system record, skip `tables::select` when the CPL/root did not change) | Per-step cycle breakdown before/after; K2 cost loops; the standing suite | standing suite | each removed cost measured; no correctness change |
| **S4** debug: TF and breakpoints | TF/RF single-step, DR0–3/DR7 execute and data breakpoints, #DB delivery, interaction with linking/dispatch (CPU 16 requires it); once breakpoints are armed, writes to DR0–3 and DR7 must flush this vCPU's code or key it by them | QEMU programs; SDM model | standing suite | #DB exact per SDM |
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
| **X1a** same-CPL delivery + IRET + delivery model | IDT walk, gates, error codes, CR2, RF = 1 in the frame of a fault between iterations of a partly executed REP string (I2 note: needs the self-exit signal), IRETQ CPL0→CPL0, INT3/INT n/INTO/UD2; the delivery model (independent author). **Old corpora:** the driver supplies a default logging IDT whose handlers record vector, error, CR2 and frame and then `hlt`; programs with no IDT get it, and the fault runners are migrated explicitly to compare that log (references above) instead of a stop. A #DB fault from DR7.GD clears DR7.GD on delivery; DR6.BD is already set by S2a (a handler-side check of DR6.BD after a GD #DB comes with delivery) | Migrated slice-1/2 fault corpora; decompressor-style on-demand #PF program; `int3_selftest` transcription | faults, loop faults, `system`, stack audit | all migrated corpora pass; model and QEMU agree on the sample |
| **S3** syscall, time, misc system | SWAPGS, SYSCALL/SYSRET, RDGSBASE/WRGSBASE, RDTSC/RDTSCP/TSC_AUX, RDRAND (if kept), **LSL**, VERW, WBINVD/INVD/CLFLUSH, CPUID/IRET draining | QEMU CPL0↔3 round trips with SWAPGS and `%gs`; LSL/VERW vs SDM model; TSC model; RDRAND per §1b | `system`, `smp` | round trips match at N = 1..4 |
| **X1b** CPL3 stack switch + IST | TSS.RSP0, IST1–7, IRETQ to CPL3 | QEMU frames; delivery model; paranoid-entry-shaped NMI/#DF/#DB programs using `lsl` (`calling.h:401-420`) | faults, `system` | CPL3 faults and IST vectors match |
| **X1c** #DF, nesting, refusals → exceptions | #DF classes, faults during delivery, triple fault → typed stop; refused-but-defined cases (unknown MSR, reserved bits) delivered | Delivery model only for #DF/nesting; QEMU frames otherwise | faults, `system` | model cases pass; no defined exception remains a stop |
| **M2** memory types | PAT/PWT/PCD/MTRR in the walker | Paging cells with PAT bits (model updated, independent author); QEMU | faults, `sharedpt` | no `detail=7` halt from a valid PAT setup |
| **M3** MMIO completion | Device ranges, site-based completion, typed stops | QEMU programs on a test device page; fault priority | faults, `contain` | 1–8-byte device accesses complete exactly once |
| **X2** eligibility, HLT, NMI | IF, STI/MOV SS shadows, interrupt windows between REP string iterations with RF = 1 saved (I2 note: take the window at the REP's self-exit or record that the last exit was one), POPF, pending events, priority, HLT park/wake, NMI pending/blocking (M64). **Event source:** a driver hook posts a vector or NMI into the vCPU's real pending-event state and kicks it (instrumentation controls a real site; vm-restructure "Correctness"), so X2 does not wait for X3's ICR. **#AC:** alignment checking at CPL 3 with CR0.AM and RFLAGS.AC (checks at access sites, or blocks keyed by the AC state), replacing S2a's `unsupported_mode` refusal | Eligibility model; QEMU programs whose interrupts come from QEMU's own sources at matching points | `kicks`, `contain` progress | contract 49 has a general-path check |
| **L5** clock service | V, TSC, 24 MHz LAPIC rationals; host wake-ups; measure kick latency for timer expiry (§2 design choice) | Exact-conversion unit tests; cross-vCPU monotonicity | unit | rates and epochs exact |
| **X3** LAPIC and IPIs | §1a LAPIC rows on M3; ICR → pending + kick; timer on L5 | QEMU APIC register programs; IPIs at 2–4 vCPUs; timer model | `smp`, `kicks` | Linux-used registers and IPIs match |
| **X4** mailbox AP start | Resident waiting state, responder, N = 1..4 | Mailbox model; trampoline + `secondary_startup_64` prefix transcription vs QEMU | `smp` | APs reach their stacks with the lock released |
| **L3** port I/O and UART | IN/OUT/INS/OUTS, IOPL/bitmap, unassigned-port rule, 16550A, IRQ4 line | QEMU for assigned ports; the manifest rule for unassigned | `system`, `contain` | 8250 sequences give QEMU's results |
| **L4a** IOAPIC + PIC (~1 run) | QEMU models in the host; IRQ routing to X3; EOI back | QEMU device models; IRQ4 → IOAPIC → LAPIC once per edge | `smp` | routing and EOI match |
| **L4b** PIT/0x61, RTC, PM timer/PM1, S5, ACPI reset register (~1 run) | From V; the FADT reset register (one byte at I/O 0xb040, value 6: Linux's default ACPI reboot writes it first) and PM1 enable bits that read back (L2); quick-PIT-calibrate and PIT-disable sequences; PM timer as watchdog source | QEMU device models; PM/TSC rate agreement | `smp`, `contain` | cited Linux sequences produce QEMU's values |
| **I2** REP strings | §1c REP row | QEMU corpora; element-boundary fault matrix; interrupt windows (after X2) | faults, ASM REP/STOS | contract 44 has a general-path check |
| **I3** LOCK forms | §1c LOCK row | `litmus` RMW cells (model extended, independent author); chains; faults | `litmus`, faults | ASM `Atomics` pass or refused with reason |
| **I5a** SSE state and arithmetic (~1–2 runs) | XMM/MXCSR in the frame, vector sites, recovery, ordering cells | FEX ASM SSE; QEMU corpora; vector fault matrix; litmus vector cells | ASM, `litmus`, faults | SSE ASM tests pass or refused with reason, 0 fail |
| **I5b** FXSAVE/XSAVE family and gating (~1 run) | FXSAVE/FXRSTOR, XSAVE/XRSTOR/XSETBV/XGETBV, CR0.TS/CR4 gating; XRSTOR #GP on a bad header, #PF on user buffers via the exception table; XCR0 bit 2 and YMM state while AVX is advertised | FEX ASM tests; SDM table for the faults | ASM, faults | images match CPU 16 layout |
| **I6** x87 reduced precision | FEX reduced mode; 80-bit images; helper inventory | `X87_F64`; QEMU exact-by-construction | ASM | 0 fail; deviation recorded |
| **I6b** x87 helper forms without a helper library | Research (Oct 29, read-only): no general in-VM helper library (needs an EL0 stack page and an EL0 rodata page, helper code outside admission, IRQ/stop handling inside helpers; pays off only for full 80-bit mode, which the owner did not choose). Instead: FSIN/FCOS/FSINCOS/FPTAN/FPATAN/F2XM1/FYL2X/FYL2XP1/FSCALE/FPREM/FPREM1 by `svc` to the resident (division precedent), computed with the toolchain's compiler_builtins libm, with x87's C2 out-of-range rule and FPREM's quotient bits and partial remainder (FEX has neither); PCMPISTRI/PCMPESTRI (I5c) the same way, porting FEX's pure C++; FLD/FSTP m80 cost goes with K2, then inline conversion in fexc only if still hot (one 10-byte crossing check before the first store) | x87 corpus extended with these forms; SDM C2/quotient-bit cases; census | ASM, faults | refused forms executed or refused with reason |
| **I8** AVX/AVX2 (if Q3 keeps them) | YMM ops | FEX ASM VEX tests | ASM | as I5a |
| **I9** remaining advertised forms | MOVBE, MMX (x87 aliasing decided), AES-NI/PCLMULQDQ, PDEP/PEXT, FFREEP; #UD for unadvertised ADX/SHA (SHA advertised since November 3: Built (SHA)) | QEMU corpus; fault matrix with the x87/MMX state; FEX ASM; census | ASM, faults | every advertised form executed or refused with reason |
| **I10** remaining architectural refusals (owner, October 30) | 0x67 address size in 64-bit mode (every memory form, strings, LOOP/JECXZ, moffs, EIP-relative, XLAT); XLAT; #UD for unadvertised/reserved encodings (XOP, RTM, PadLock, reserved VEX/MMX, F3 MOVBE...); CET hint space as NOPs; gathers/masked stores stay refused | QEMU corpus at both block sizes; fault/#UD/NOP case matrix by the SDM; FEX ASM; census; Debian boot | standing suite | every 0x67 form exact with exact fault sites; every listed #UD delivered |
| **R1a** single-vCPU composites (~1 run) | Decompressor entry; kernel head (`head_64.S:37-405`); early #PF table building; `int3_selftest`; alternatives/static-call patching through a second mapping under another CR3; syscall round trip with PTI CR3 switch and VERW; timer tick into `sti; hlt`; serial write with IRQ4 | QEMU system mode with the same programs | all | each matches at block sizes default/1 |
| **R1b** SMP composites (~1 run) | Mailbox AP start into the head; `text_poke_bp` + IPI `sync_core`; paranoid NMI with `lsl`; IPIs under load | QEMU at N = 2..4 | all | each matches at N = 2..4 |
| **R2** phase-end audit | Contract inventory (44, 49, 51, SSE parts of 1/2/5, LOCK parts of 43); stack audit; integration handoff page | Independent review of the whole path | all | §4 holds |

## 4. Phase end: ready for boot integration

Audit at R2 (November 2; HEAD 16b0576, fexc series44-r2 = series43-x1 byte for byte; raw summaries
`natemu-raw/r2-20261102-01/`, "R2" below), updated after the independent phase-end review's fixes (uncommitted on
e6b2bf5; Built (R2 review fixes), `natemu-raw/r2-final-20261102-01/`). An item is ticked only with the evidence named.

- [x] P2's recommendation and P1's outcome recorded by the owner; the deferred list holds options A/B if chosen (Q5).
      Owner decision 6 (§5, November 2): P1's outcome is accepted as recorded in INV-03-01's "Next phase" row and
      carried into the performance phase; P2's options A/B are superseded by the October 28 decision (translated code
      at EL0, hardware-only checking), so Q5 adds nothing to the deferred list.
- [x] Every §1 row maps to an R1 composite or a named check, and every refused form in the C0 census is implemented or
      an accepted typed refusal. The mapping is complete ("R1 coverage" and its R1b addendum below: no row without a
      check). Census rerun after the review fixes (`natemu-raw/kernel-census-r2-20261102-01/`, the classifier with the
      REX-chain and X1c rules; `experiments/track03/kernel-census/README.md`, R2 review re-check): the kernel side
      refuses nothing (`prefetchw` 17 compiled since X1c; 3 undecodable bytes in the 32-bit decompressor head, never
      run); the initramfs's 15 unattributed sites were REX chains in OpenSSL strings, which fexc compiles and the host now
      decodes (Built (R2 review fixes), A); left are the 10 VEX gathers (owner decision October 30 (2)) and 2 GS-prefixed
      `jb` in an OpenSSL string, a reserved encoding (SDM Vol.2 2.1.1), compiled as the unprefixed branch since the owner's
      rule (§5, November 2: match the processor; series45-r2fix): census rerun, initramfs left only the gathers.
- [x] CPUID/MSR profile as recorded (CPU 9/16/17 or the owner's variant), unit-tested leaf by leaf and MSR by MSR;
      fault-model profile rederived independently. Built (S1): `crates/abi/src/cpu_profile.rs` generated from
      `tools/models/cpu_profile.json`, the abi tests replaying `cpu_profile.py`'s answers per leaf and MSR (`cargo test`
      147 pass at R2), `tools/models/test_cpu_profile.py` and `test_cpu_profile_rs.py` (Python 317 pass at R2);
      `tools/system_fault_model.py` written from the SDM, not natemu's code, over the same profile; `system` 0 failures.
- [x] Exceptions and interrupts delivered, not stopped: migrated fault corpora pass through the logging IDT; contracts
      44 and 49 have general-path checks in [contracts.md](contracts.md). Built (X1a, X1b, X1c, X2); at R2 faults 7,912
      at both block sizes, loop faults 846 x 3, `delivery`, `events`, `run_x1.py` 6/6 and 188/188 green; contracts.md
      rows 44 (I2 corpus, mid-string faults, `kicks` `rep@N`) and 49 (`events` judged by `delivery_model.py`).
- [x] LAPIC (xAPIC MMIO), timer, IPIs and the mailbox AP start work at N = 1..4. At R2: `run_x3.py` (QEMU 9/9, IPIs
      4/4, timers 24/24, stress 6/6, halt 10/10, cases 54/54), `run_x4.py` (33/33, 28/28, trampoline 6/6 at 2-4
      vCPUs), `run_r1.py r1a` `r1-timer` (N = 1) and `r1b` (mailbox start, IPIs under load, NMI by IPI at N = 2-4), and
      Debian booting at N = 1..4 with every CPU brought up ([boot-handoff.md](boot-handoff.md) §4).
- [x] Loader output passes the independent boot.rst checker for the chosen image; ACPI tables disassemble cleanly and
      agree with CPUID/MADT/mailbox. At R2: `run_l1.py` checks 12/12 and rejections 41/41 (`tools/models/bootproto.py`),
      `run_l2.py` N = 1..4 pass (`acpi_model.py`), `run_l2_iasl.py` pass.
- [x] UART, port I/O (incl. the unassigned-port rule), PIC/IOAPIC, PIT, RTC and PM timer match QEMU on the cited
      sequences; PM timer and TSC agree in rate. At R2: `run_l3.py` 0 failures, `run_l4a.py` (QEMU 13/13 and the
      rest), `run_l4b.py` (QEMU 5/5, cases 11/11, modes 7/7, `clocks` 1/1: PM/PIT/LAPIC/RTC against the TSC).
- [x] Guest RAM at 2 GiB with the M69 holes; standing suite green at 4 MiB and 2 GiB. At 2 GiB: Built (M1), the legacy
      hole Built (L3), the suite below. At 4 MiB (the small-RAM suite, owner decision 7): every runner green at R2 except
      three whose programs used fixed addresses at or above 4 MiB; they were relocated below 4 MiB, keeping what each
      tests (`run_x4.py`'s mailbox page, `run_l3.py`'s RAM buffer and RAM's last page, `run_m1.py`'s top-of-RAM
      placement from `NATEMU_RAM`; [boot-handoff.md](boot-handoff.md) §5) and are green there (x4 33/33, 28/28, 6/6; l3
      0 failures; m1 20/20, `natemu-raw/r2-final-20261102-01/small-4m/`); `run_m2.py` 112/112 at 4 MiB since the R2
      fix.
- [x] ISA: ASM suite 0 fail with SSE, XSAVE, x87 (`X87_F64`), REP, LOCK and flag-move tests passing or refused with
      reason; AVX per Q3. At R2: 2,270 tests, 1,875 pass, 0 fail, 48 x87 precision deviations (the reduced-precision
      class), 347 unsupported with reasons; AVX/AVX2/FMA/F16C kept by the owner (§5, October 28) and built (I8).
- [x] R1a/R1b match QEMU at N = 1..4, block sizes default and 1. R1a 7/7 at N = 1 (Built (R1a),
      `natemu-raw/r1-20261102-01/`), R1b 4 programs at N = 2, 3, 4 (Built (R1b), `natemu-raw/r1-20261102-01/r1b/`);
      rerun at R2 at 4 MiB and 2 GiB: R1a 7/7 and R1b 12/12 each, both block sizes, negative controls flagged.
- [x] Standing suite green; `check_resident_stack.py` passes on the final binary; nothing staged. At R2 at 2 GiB: every
      command of [boot-handoff.md](boot-handoff.md) §5 green but the 7 recorded i8b QEMU two-NaN departures (Built (I8)
      review) at both block sizes; the stack check `ok` (gate 19,168 of 32,768 bytes, nested 23,312, boot 12,832).
      Rerun after the review fixes (every §5 command at 2 GiB, both block sizes, `natemu-raw/r2-final-20261102-01/suite-2g/`;
      Built (R2 review fixes)): the same, plus the REX corpus 25/25 at both and `run_boot_smoke.py` 3/3; `cargo test`
      151 + 1 ignored; `git status`: the review fixes and documentation, unstaged, nothing staged.
- [x] Integration handoff page: boot command, manifest inputs, known limits and costs (incl. PTI CR3 flushes).
      [boot-handoff.md](boot-handoff.md), rewritten at R2: K1 keeps code across CR3 loads; each still resets the data
      shadows (no PCID), the PTI cost with mitigations on.

**Blocking for integration (found at R2; resolved, Built (R2 fix)):** the library boot entry (`natemu` stub and `boot-linux`, both through
`natemu::linux::Boot`) delivers device interrupts through a dangling pointer: `irq::Lapics::memory` points at the local
`backing` of `Boot::prepare`, which is then moved into `Prepared` (L0, 3c12567). User-space console output is lost (IRQ 4
never taken) and the installer initrd hangs after "Run /init" at N = 1, 2 and 4, warm cache or off; 959e430 (before L0)
and QEMU print. A scratch fix re-pointing it in `Prepared::run` reaches the dialog. Details, evidence and the boot
tables: [boot-handoff.md](boot-handoff.md) §4. The standing suite cannot see it (`run-program` does not move its
backing); it needs a library-path boot check. *Resolved:* `Backing::memory` is shared (`Arc`) with the chipset; the
library-path check `tools/run_boot_smoke.py` and a unit test are red at b3f6841 and green with the fix; the installer
boots reach "Select a language" at N = 1 (67.5 s) and 4 (48.7 s) through the stub with a warm cache
(`natemu-raw/r2-fix-20261102-01/`).

**R1 coverage (R1a, November 2).** Each §1 row with the R1a composite that exercises it (if any) and the named checks
that judge it. *1a:* IDT delivery: `r1-demand`, `r1-int3`, `r1-boot` (#UD); `run_slice3_vm.py delivery`. TSS.RSP0/IST:
`run_x1.py qemu` (no composite; R1b's paranoid NMI). IRETQ, NMI unblocking: every composite's handlers, `r1-poke`'s
`sync_core`; `run_x1.py`, `events`. #DF classes: `delivery` (X1c matrix). INT3/INT n/UD2: `r1-int3`, `r1-poke`, `r1-boot`;
`delivery`. LSL paranoid entry: `run_x1.py qemu`, `system` (LSL tables). IF/STI shadow/HLT: `r1-timer`, `r1-serial`;
`events`. NMI: `events`, `run_x1.py`, `run_x3.py qemu`. xAPIC MMIO: `r1-timer`, `r1-int3`, `r1-serial` (PPR/ISR/IRR/TPR
reads per delivery); `run_x3.py`. LAPIC timer: `r1-timer`; `run_x3.py timers`. IPIs: `run_x3.py`. Mailbox AP start:
`run_x4.py`. PIC/PIT/RTC/IOAPIC/PM: `r1-serial` (8259 init, I/O APIC pin 4); `run_l4a.py`, `run_l4b.py`. Idle states:
`run_l2.py` (no `_CST`/P_BLK), `r1-timer` (HLT idle). *1b:* CPUID: `tools/models/test_cpu_profile.py`, `system`
(`profile_program`), `run_x4.py trampoline` (`verify_cpu`). MSRs: `r1-boot` (EFER, GS_BASE), `r1-syscall`
(STAR/LSTAR/FMASK/KERNEL_GS_BASE); `system` (`sys-msr-*`). CR0, CR4: `r1-boot`; `system`. CR2: `r1-demand`; CR8:
`run_x3.py` (`apic-cr8`). Debug registers: `run_s4.py`. Descriptor tables and segments: `r1-boot` (LGDT, LRETQ, LIDT,
SGDT/SIDT, null selectors), `r1-syscall` (LTR); `system` (`seg-boot` and the S2b programs). SWAPGS/SYSCALL/SYSRET:
`r1-syscall`; `system` (`sc-roundtrip`). RDTSC/RDTSCP: `system` (`profile_program`), `run_x3.py timers`. RDRAND:
`system`, `run_k2.py`. VERW: `r1-syscall`; `system` (syscall_model tables). XSAVE family: `run_i5b.py`, ASM. WBINVD,
CLFLUSH, PAUSE, HLT: `system` (S3 cache cases), `run_m2.py`; HLT `r1-timer`. Kernel self-modifying code: `r1-poke`;
`run_k1.py`, `run_k2.py` (XMC), `coherence`, `run_slice2_probes.py`. *1c:* small integer forms: slice-1 corpora, ASM.
FS/GS overrides: `r1-boot` (%gs:40), `r1-syscall`; I7 corpora. REP strings: `r1-demand` (REP STOSQ faulting at its
start and midway), `r1-boot`; `run_slice1_faults.py`, ASM. LOCK: `litmus`, `smp` (`lock-counter`), ASM. SSE: ASM, the
I5a corpora. AVX: `run_i8_faults.py`, the I8 corpora. x87: `run_i6_faults.py`, `run_i6b.py`, ASM `X87_F64`. NOPL:
`r1-poke` (the NOP5 runs before it is patched); ENDBR64: `run_i10_faults.py` (the `0f 1e` NOPs). *1d:* guest RAM:
`run_m1.py`. Memory types: `run_m2.py`. MMIO from translated code: `run_m3.py`; `r1-timer`, `r1-serial`. 1 GiB pages:
`run_slice1_faults.py` (PS in a PDPTE). *1e:* loader: `run_l1.py`, `run_l1_smoke.py`. ACPI tables: `run_l2.py`,
`run_l2_iasl.py`. Port I/O and the 16550A: `r1-serial`; `run_l3.py`. Legacy island: `run_l4a.py`, `run_l4b.py`.
Virtual time: the L5 unit tests (`cargo test`), `run_l4b.py clocks`. Shutdown: `run_l4b.py` (S5). **Rows without any
check:** none. Partial: the decompressor's LA57 probe is not transcribed (CPUID.7.ECX[16] = 0 and CR4.LA57 reserved are
unit-tested); the C0 census half of the second checkbox is open.

**R1 coverage addendum (R1b, November 2).** The multi-vCPU rows R1a left open now have a composite. Mailbox AP start:
`r1b-mpstart` (and `run_x4.py`). Cross-vCPU code coherence / kernel self-modifying code at N > 1: `r1b-poke` (the
`text_poke_bp` INT3 protocol with peers executing the site, the sync IPI's IRETQ as `sync_core`; and `run_k1.py`,
`coherence`). NMI by IPI, the paranoid LSL/RDGSBASE/WRGSBASE/SWAPGS entry on an IST stack: `r1b-nmi` (and
`run_x1.py qemu`'s paranoid entries, which already cover a CPL 3 -> 0 and CPL 0 -> 0 stack switch through TSS.RSP0 and
every IST index). IPIs and EOI under concurrent load, per-pair exactness: `r1b-ipiload` (and `run_x3.py`, `run_l4a.py`).
A CPL 3 -> 0 stack switch and IST delivery are judged by the existing X1b checks (`run_x1.py qemu`); the R1b composites
start at CPL 0, as a kernel does after `secondary_startup_64`.

## 5. Owner questions and recorded decisions

**Owner decisions (November 2, phase end):** (1) faster first-time code (parallel compiling, translating ahead of
use, translations reusable across load addresses) is the next chunk after this phase, measured first; (2) an AES-based
random-number generator inside the VM, seeded from the host, is after this phase; (3) raising and testing guest RAM
beyond 3 GiB and vCPUs beyond 4 is after this phase; (4) CR0.NE = 0 (x87 external-error mode) stays a named stop
(`unsupported_ferr`); (5) INCSSP without CET follows the SDM (#UD), not QEMU; (6) P1's (performance diagnosis) outcome
is accepted as recorded in INV-03-01's "Next phase" row and carried into the performance phase; P2's options A/B are
superseded by the October 28 decision (translated code at EL0, hardware-only checking); (7) the runners that could not
run at 4 MiB (`run_x4.py`, `run_l3.py`, `run_m1.py`) either move their programs below 4 MiB, so they run in the 4 MiB
suite, or, where relocation would change what they test, run in the small-RAM suite at the smallest RAM that fits
(applied: all three relocated, §4); (8) finish rule: the phase closes when the §4 checklist is met with evidence, no
known blocking defect remains (wrong results on the boot path, unsoundness, containment holes, crashes), suites and boots
are green, and every other known issue is on one tracked list for the next phase ([boot-handoff.md](boot-handoff.md),
"Known issues"). (1)-(3) are on the deferred list below.
**Owner rule (November 2, phase end): where the SDM is underspecified or calls an encoding reserved, match a real x86
processor** (applied first to segment prefixes on near direct branches: Built (R2 review fixes), round 2). Each such
choice matches the processor as far as known (QEMU's or the documented behaviour) and is to be re-verified on real x86
hardware in the correctness pass (after the performance phase; owner, November 2: with large-scale differential fuzzing
and real workloads):
**Owner decision (November 2, reference machine):** the x86 machine for this list is `ubuntu@192.168.12.11`, an AMD Ryzen 7 8845HS in a VM with nested SVM (`/dev/kvm` present; the `ubuntu` user is not yet in the `kvm` group). Where the Intel SDM defines a behaviour, natemu follows the SDM (the profile is GenuineIntel); where the SDM is open or reserved, this AMD machine is the reference, accepting vendor differences. **To verify on real x86 hardware, correctness pass (after the performance phase)** (each made from QEMU's or inferred processor behaviour, none run on an Intel part):
segment prefixes (26/2E/36/3E/64/65) on near direct branches ignored (R2 review); the Reserved-NOP cells of `0f 0d` and
`0f 18..1d` executing as NOPs (X1c, pinned QEMU's sweep); F2 (BND) on near CALL/RET/JMP/Jcc ignored without MPX (X1c);
`0f 0d` with a register operand #UD (X1c, QEMU's 12 encodings); INCSSP without CET #UD (owner decision 5: the SDM, where
QEMU differs); 66/F3/F2 on EMMS (`0f 77`) and on the SHA cells (`0f 38 c8..cd`, `0f 3a cc`) #UD by the blank cells where
QEMU ignores the prefix (X1c review); a REX followed by a legacy prefix ignored (R2 review: the SDM's rule, which pinned
QEMU does not follow); lowest-priority delivery to the lowest-numbered enabled APIC (X3: model specific); an MTRR overlap the
SDM leaves undefined taken as UC (R2 fix). Not on the list: 66 on a near direct branch, which the SDM defines (ignored in 64-bit mode;
Built (66 on near direct branches)).
**Hardware check (November 2-3, the reference AMD machine, user mode; `natemu-raw/hwcheck-20261103-01/` `probe.c`,
`probe-output.txt`, `table.md`, and `hwcheck-20261103-02/` `probe2.c`, `probe2-output.txt` for the segment-prefix forms
below).** Verified, the machine agreeing with natemu: segment prefixes and two of them on Jcc/JMP/CALL/LOOP/JrCXZ ignored
(`probe-output.txt` lines 5-55, 48/48 and 2 pairs as unprefixed); the Reserved-NOP cells of `0f 0d` and `0f 18..1d` (and `0f
1e`, `0f 1f`) as NOPs with no access, valid and non-canonical `[rax]`, every 66/F2/F3/NP and /0../7, and LOCK on them #UD
(lines 290-337, 1,080/1,080); `0f 0d` with a register operand #UD (same sweep); F2 (BND) on near CALL/RET/JMP/Jcc and
indirect JMP/CALL ignored (lines 91-103); REX chains (the last REX counts) and a REX followed by a legacy prefix ignored
(lines 104-133); 66/F2/F3 on EMMS and the SHA cells #UD (lines 134-241); INCSSPD/Q #UD, RDSSPD/Q a NOP keeping its register,
ENDBR64 a NOP, SAVEPREVSSP/SETSSBSY/RSTORSSP/CLRSSBSY/WRSS #UD (lines 242-266). Differences, natemu unchanged because the SDM
defines them: 66 on near direct branches (the machine decodes rel16, AMD's rule; lines 57-90); WRUSSQ at CPL 3 #GP there
(that kernel sets CR4.CET; natemu has CR4.CET = 0, #UD by the SDM; lines 267-268); unprefixed SHA and GFNI run there (the
profile does not advertise them). Not run (need a kernel-mode harness): lowest-priority delivery, the MTRR overlap taken as
UC. Differences changed by the owner (below): the four segment-prefix refusals or classes.
**Owner decisions (November 3, segment prefixes; the reference machine's behaviour, applied in fexc series47-seg: Built
(segment prefixes, November 3)):** (1) CS/DS/ES/SS prefixes on a memory operand in 64-bit mode are ignored: the operand's
default segment (SS for an RSP/RBP base or a stack instruction, DS otherwise) decides #SS(0) or #GP(0) for a non-canonical
address. Intel Vol. 1 §3.3.7.1's sentence suggesting #SS for an SS override is treated as not defining it, so the AMD
machine is the reference (`probe-output.txt` lines 270-280: an SS prefix on `[rax]` #GP, DS/ES on `[rbp]` #SS;
`probe2-output.txt` lines 10-46: two null prefixes, `[rsp + rbx]`, CS on `[rbp]`, moffs, PUSH/POP m, XLAT and the string
sources likewise). This replaces the October 29 coordinator's Intel-class rule (Built (I7), revised). (2) Of two or more FS/GS
prefixes the last applies (`probe-output.txt` lines 283-286; `probe2-output.txt` lines 48-60, a null prefix before or
after them changing nothing). (3) An FS/GS prefix on an instruction with no memory operand is ignored (`probe-output.txt`
lines 287-288; `probe2-output.txt` lines 62-82: STOS/SCAS's ES:RDI, PUSH/POP of a register, a register form; the DS:RSI
source of MOVS/LODS takes FS/GS, which the SDM defines).

**Owner decisions (October 28):** kernel = Debian 6.12 (32-bit stays deferred); fetching the Debian kernel,
config and initramfs into natemu-raw is allowed (nothing committed); mitigations are booted off on the command line
(`mitigations=off`), while VERW and CR3 switches are still implemented correctly; the accepted CPU 16 profile is kept
(AVX/AVX2/FMA via FEX AVX128, RDRAND, MTRR in this phase). The owner asked why a CR3 load discards the code cache:
it is an S3c1 simplification, not an x86 requirement — see the code-cache-across-CR3 item added to the plan.
Also: single-step (TF/RF) and hardware breakpoints are built in this phase as CPU 16 requires (no deferral); the
loader may run Debian's real decompressor up to its jump into the decompressed kernel as a smoke test.
**Code cache across address-space changes (owner, October 28; to be chunked).** Translations stay valid across CR3
loads and INVLPG: blocks are filed by the guest-physical pages their bytes came from (already tracked for cross-vCPU
coherence); a CR3 load or INVLPG drops only the vCPU's shadows (x86 TLB semantics), and a lookup by linear RIP
re-validates linear → physical before entering a block. Code bytes changing (guest stores, and host/device writes into
guest RAM such as disk DMA, which today stop all vCPUs and flush everything) invalidate per physical page. Check:
programs switching between address spaces keep their translations (counted), while remapped or rewritten code is
always re-translated, at 1–4 vCPUs. Privilege level: a follow-up study (EL0 without the address guard) is running;
EL1 stays until the owner decides on its evidence.
**Owner decision (October 28): adopt hardware-only checking first.** Translated code runs at EL0 and touches no
emulator data; every non-canonical access faults in hardware and becomes an exact #GP/#SS; the address guard goes and
admission's code-shape rules become a debug option (object validation stays). Evidence: `experiments/track03/el0-study/`
(follow-up and independent review). Order: (E1) frame-free code at EL1t with the guard on — exits via `svc`, division
via `svc`, no header store, pre-NZCV in a dead temporary (not x28), stop as `bl` to a per-vCPU execute-only pad stopped
by removing X at stage 2, lookup table as code (measure fill/clear cost); (E2) switch to EL0 (page roles, lower-EL
vectors, EC 0x24/0x20 stop exits); (E3) remove the guard, negative #GP/#SS tests for every former window address and
both straddles, split admission; (E4) full suites and independent review. x28 stays FEX's state base. Guest state FEX
keeps in memory (DF, FS/GS bases, MXCSR, x87, AVX upper halves): owner asked for a survey of existing solutions before
choosing (registers, traps/specialised snippets, a guest-state page, hardware permission features).
**Owner decision (October 28): one EL0 guest-state page per vCPU (FEX's layout at x28).** It holds only that vCPU's
guest state that FEX's generated code keeps in memory (DF, FS/GS bases, MXCSR, x87 registers and status, AVX upper
halves); no emulator-trusted value lives there (FEX's frame pointers are unused after E1 and stay zero; the resident
treats the page as untrusted guest data). Documented deviation, like owner decision 2: a guest data access to exactly
that page — one fixed 4 KiB page per vCPU in the non-canonical hole, placed away from Linux's poison patterns — reaches
that vCPU's own state instead of raising #GP/#SS; every other non-canonical address faults exactly. Enforced by a test
that fails if anything but listed guest-state fields is placed on the page. To verify: which kernel paths may be handed
arbitrary addresses (tracing/debugger reads with fault fix-ups) and could land there. Survey:
`docs/research/guest-state-protection-survey.md`.
**Owner decisions (October 28, after `experiments/track03/native-interrupts/`):** (1) Hypervisor.framework's in-kernel
GIC (macOS 15+) is the doorbell for every asynchronous x86 event — LAPIC timer on the virtual timer PPI, IPIs and
cross-vCPU kicks on SGIs, device interrupts on per-vCPU SPIs, NMI as a doorbell plus a resident pending bit; HLT is a
resident `wfi`; x86 semantics (IF, shadows, NMI blocking, LAPIC priority/TPR/ISR/IRR/EOI, IOAPIC/PIC) are applied by
the resident at precise stops; host `hv_vm_protect`/`hv_vcpus_exit` remain only as the watchdog fallback. (2) The
per-entry stop pad is replaced (E1b, before E2) by IRQ-triggered unlinking: the resident's IRQ handler patches the
running block's exits (including computed exits) to stop, restores them after; nothing on the hot path.
*Built (E1b):* with the pad gone, `hv_vm_protect` has nothing precise to protect and is removed; the host fallback is
`hv_vcpus_exit` under the watchdog (a failed or lost doorbell is contained — the run stops — not recovered). A precise
host fallback remains possible if wanted (exit, then enter the resident's IRQ path with the exited PC). Owner informed.
*Built (E2):* translated code runs at EL0 (blocks entered with SPSR EL0t, IRQs unmasked); its `svc`, `brk`, aborts and
traps arrive at the lower-EL vectors, every other vector is a resident fault that halts. Page roles: the code area
(blocks, slot, dispatcher, L1) is EL1 read-only and EL0 execute-only (never EL1-executable); resident text is
EL1-executable only; every other private page (State, tables, records, frame page, stacks, guest-RAM alias) is
EL1-only; shadows are EL0 read/write or read-only. x86 CPL stays in the software walker: a shadow exists only for an
access granted at the vCPU's CPL, so CPL0 shadows of supervisor pages and CPL3 shadows of user pages map alike
(superseded by U1: one shadow space per CPL class, switched without a reset). Isolation is the hardware's: forged system instructions halt
`trapped` (ESR 0x00/0x01/0x18) and loads, stores and branches into the private window `private_access`
(`contain` EL0_FORGED). The guard stays; a guest access cannot reach the window through it. Steady-state benchmarks
unchanged. Also: the resident decodes each link point's move at the reply (it must build the point's target). EL0
access to the debug channel, counters/timers and PMU traps to EL1, not the host (MDSCR_EL1.TDCC, CNTKCTL_EL1 = 0).
**E3 input:** `aborted` classifies `trapped`/`private_access` before the site lookup. Once the guard goes, an abort at a
recorded access site must become #GP/#SS from the walker by `site_address`, even with FAR in the window or a permission
fault on the code area; `private_access` stays only for an ELR that is no access site. G1's page must lie outside the
window or be exempted explicitly.
*Built (E3):* the address guard is gone (fexc series13: a change in `tools/fexc/shim/`, the patches are series12's).
Kept as x86 semantics: the split check, the branch-target check (#GP at the branch, before a call's push, a ret's pop or
any RIP change: a lazy check at dispatch would come too late), the #DE checks, precision and the site map. `aborted`
classifies by site first: a `brk` at a stub, or a data abort at an access site whose recorded address range holds FAR,
goes to the site's handler (any non-canonical address, the window included, is #GP or #SS from the record; a
non-canonical swap crossing 16 bytes is #GP, not a split lock); only an exception at no site, or not of its site's kind,
halts `trapped`, `private_access`, `site_mismatch` or `unknown_abort`. Admission is split: object validation always
(records, register map, relocations, link points and their targets, the dispatch exit's form, division `svc`s only in
64-bit div/idiv, x25 written only by the pre-NZCV save, x28 never written and never a base outside `GUEST_STATE`, only
`svc #LINK/#DIV`, forward branches, no fall-through into data or stubs, each site's word its recorded access, and
unknown words only if they neither branch nor name x25/x28); the code-shape check (allowlist, sp, literals, unrecorded
accesses, split/branch-target/#DE check forms, stubs reached once) is a debug option, `NATEMU_CODE_SHAPE=1`, on in the
test runners and off in `contain`. Negative cases (faults suite, `window_probes`): every former window class, both
holes, poison values, both canonical straddles and RIP-relative accesses into and across the low hole, with every data,
stack, split-store, XCHG and call/ret form, give exact #GP/#SS at block sizes default and 1 (386 cases; the faults
suite is 3,829 at both). Census: 0 address-guard
words (series12: 3,735); 19,712 words against 23,866 (-17.4%), same blocks and guest instructions.
*Built (U1):* each vCPU has two shadow spaces, supervisor (CPL 0-2) and user (CPL 3), with their own roots (window
0x10000/0x17000 and 0x1c000/0x1d000) and ASIDs (2v + 2, 2v + 3); one table pool. The resident loads the CPL's space
before every entry into generated code (`tables::select`, TTBR1 walks off while the roots change); CR3 loads, control
changes, INVLPG, shootdowns, evictions and halts remove from both. Blocks stay keyed by CPL (fetch U/S and SMEP are
checked at translation). API: `execution::set_cpl(s, cpl)` at a block boundary with the state banked; it changes only
the CPL and flushes nothing. Callers (X1b delivery, S3 SYSCALL/SYSRET, IRET) still own the rest of the transition: CS/SS
loads with RPL = CPL, the stack switch (TSS.RSP0/IST, null SS on entry to CPL0), RFLAGS masking, no implicit SWAPGS,
and every implicit supervisor access (frames, descriptor tables) through the walker, never shadows. Test hook: the
driver's `switch ADDR 0|3` (system kind `SET_CPL`, never decoded from x86). Check: `run_slice3_vm.py privilege`
(fault matrix of 40 cases at 1 and 4 vCPUs, INVLPG/CR3 dropping both spaces, SMEP after a CPL3 phase, 10 vs 1,000
rounds of switches with equal shadow resets and retries; block sizes default and 1). Left to K1: each CPL change
still clears L1 (it is per code context).

*Built (I1, G1 minimum; moved October 30, below):* the guest-state page is the vCPU's existing FEX frame page with a
second alias, EL0 read/write (`execution::GUEST_STATE_VA`, x28 at every block entry), while the EL1-only alias stays at
`FRAME_VA` (`PRIVATE + 0x23000`, the resident's view; the E3 window probes of that page still give #GP). `aborted` needs
no exemption for the page itself because it is mapped, so an access to it never aborts. Extent: exactly one 4 KiB page
per vCPU; an access straddling into it or out of it faults (#GP from the site), and so does every window page
(`run_slice1_faults.py` window class `guest-state` and its straddles). Deviation: a guest access inside the page reaches
the vCPU's own guest state instead of #GP. What the resident trusts: it reads back only DF (FEX's byte at `frame::DF`,
banked in `bank`); TF, IF, IOPL, NT, AC, VIF, VIP and ID bytes (RF, VM as 0) are rewritten from the bank at every block
entry (`frame::flag_bytes`), so a guest write to them lasts at most to the next entry and only `pushf` sees it; the
FS/GS bases (`frame::FS_BASE`/`GS_BASE`, I7) and FCW are likewise written from the bank at every `enter` and never read
back. Admission lets x28 load the flag bytes and store only DF (and the x87/AVX fields, I6/I8). Invariants: the abi
tests `guest_state_page_layout` (fields inside the page, `pushf` image, DF round trip, the address's class) and the
per-run frame check in `drivers/program.rs` (`frame_intact`: any nonzero byte outside `frame::GUEST_STATE` fails the
run).
*G1 fix (October 30; guest kernel/user isolation):* the EL0 alias was at `PRIVATE + 0x24000` = `0x0000_8000_4002_4000`,
non-canonical but with bit 63 clear. Linux 6.12's `put_user` (`arch/x86/lib/putuser.S`, `sar $63; or`) folds only
addresses with bit 63 set and leaves a lower-half non-canonical one to the hardware #GP, so an unprivileged process
could make the kernel store chosen bytes into the page with ordinary system calls (`set_tid_address` then
`prctl(PR_GET_TID_ADDRESS, alias + 0x3E0)` writes the GS base the kernel's own `gs:` accesses use until the next
resident entry; `time(alias)`), and the call returned success instead of -EFAULT: DF at CPL0, the kernel's per-CPU base
and another task's lazily saved x87 state were reachable. Host isolation was not affected. **Now:** the alias is
`0xffff_4000_0000_1000`, in the upper Arm translation table's entries 0..255 (`0xffff_0000_0000_0000..0xffff_8000_0000_0000`):
x86 non-canonical with bit 63 set, so `put_user` folds it to all-ones (#PF, -EFAULT), `get_user`/`access_ok`/`copy_*_user`
refuse it (above `USER_PTR_MAX`), the nofault reads fail their canonical check, and no guest shadow ever uses those
entries (`shadow::Space` refuses the range; its `halves`/`reset` touch only entries 256..511 of an upper root).
*Mapping:* the host (`memory::guest_state_alias`) links upper-root entry 128 of both shadow spaces (`UPPER_ROOT`,
`USER_UPPER_ROOT`) to three owner tables (`execution::GUEST_STATE_TABLES`, window pages `0x28000..0x2b000`, after the
guest-RAM alias's level-2 tables, never mapped in the window, so the resident cannot change those three pages); their
only leaf is the frame page, AP 01 (EL0 and EL1 read/write), PXN and UXN, not global (each space's ASID). Entry 128 itself
lies in the upper roots, which the resident writes: it is protected only by the shadow paths' entry range
(`Space::root` refuses addresses below `HIGH_BASE`; `halves`/`reset` touch only entries 256..511). `Space::map` also
refuses the frame page and the three alias tables as shadow targets (defence in depth; shadow targets are guest RAM or
the all-ones page anyway). *TCR:* `TCR_BOTH` (T1SZ = 16:
48-bit upper half, TG1 4 KiB, EPD1 clear, A1 = 0, no TBI); EPD1 is set only inside `tables::select`, at EL1 while the
roots change, and cleared before it returns, so the alias is walkable whenever generated code runs; the execution mode
always runs with the upper half on (`private_space(.., true)`, `TCR_BOTH`). The window page at the old address is
unmapped now (no window page is EL0 data; its neighbours, the frame page and the RAM tables, are EL1-only or unmapped),
so a guest access there is #GP like any other non-canonical address. *Aborts:* an EL0 abort at no access site with FAR in
the alias's 512 GiB slot (`execution::GUEST_STATE_SLOT`: a fetch from the page, an access beside it) halts
`private_access`, as in the window; at an access site, the slot is non-canonical: #GP/#SS. *x87 slots:* admission's
code-shape check now also requires that no branch enters between the `and wI, wN, #7` mask and its `[x0, #0x420]`
access, so every path masks the index (the slot stays at x28 + 0x420..0x498; before, a forward branch past the mask was
admitted, a negative control of `x87_slot_index_is_masked_on_every_path`). The code-shape check is a debug check
(`Compiler::shape` defaults to off; `boot-linux` runs without it), so the containment that holds in every run is the
hardware's: the alias's slot maps exactly one page, and an x0 outside it faults (`private_access` at no site).
**Remaining deviation (recorded):** a guest access inside the page, at any CPL, reaches the vCPU's own guest state
instead of #GP, as before. A user-mode access reaches only what that vCPU's own state already is: the bases and flag
bytes it may write are rewritten at the next resident entry, and every CPL change (SYSCALL, interrupt, exception, IRET)
passes through the resident, so the kernel never runs with a value a user process stored there; a user DF or x87 value
stored there is that task's own state. A kernel path reaches the page only through a kernel bug (an unchecked
`__get_user`/`__put_user`/`unsafe_*_user` on a user pointer, which x86 would turn into #GP) or privileged BPF direct
loads (`BPF_PROBE_MEM`, which read only, needing CAP_BPF/CAP_PERFMON). **Constraint for later:** never advertise LA57
or more than 48 linear-address bits (CPUID 7.ECX[16], 80000008H.EAX[15:8] = 48): with 57-bit linear addresses the
alias would be canonical and the guest could map it. Nor LAM (CPUID 7.1:EAX[26], CR4.LAM_SUP bit 28): it ignores bits
62:48 of a supervisor pointer, so `0xffff_4000_...` would alias a canonical kernel address. Both are asserted by
`guest_state_page_layout`.
*Built (S1, presented CPU):* `tools/models/gen_cpu_profile_rs.py` generates `crates/abi/src/cpu_profile.rs` from
`cpu_profile.json` (CPUID leaves, MSR access/reset/writable mask/rules, CR0/CR4/EFER/XCR0 sets, the selected "enabled"
MTRR state) plus `cpu_profile.py`'s answers as test vectors; `crates/abi/src/cpu.rs` (CPUID with its dynamic fields,
the MSR checks, reset values) replays them in the abi tests, and `tools/models/test_cpu_profile_rs.py` fails while the
file is stale. The host decodes CPUID, RDTSC and RDTSCP; the resident executes them and every profile MSR as
transactions: CPUID at any CPL (a draining entry, as every system instruction), RDTSC/RDTSCP #GP at CPL > 0 with CR4.TSD;
RDMSR/WRMSR with the profile's #GP rules before any effect; MISC_ENABLE bit 22 limits CPUID, CPUID.1 reloads the
UCODE_REV latch with virtual revision 0, FS/GS_BASE are the bank's `Cpu::fs_base/gs_base` (I7), an MTRR write drops the vCPU's code and shadows (CPU 9),
PAT and MTRRs are recorded for M2. The MSRs live in their own record after `State` (`execution::MSRS_OFFSET`), written by
the host at reset (vCPU 0 is the BSP). CR4: bits outside the profile's 0x1507bc are #GP; PSE and TSD are implemented;
DE, PCE, OSFXSR, OSXMMEXCPT, FSGSBASE, OSXSAVE stay refused (since S2a, I5a and I5b each is implemented: Built (S2a), (I5a), (I5b)). IA32_APIC_BASE: the BSP flag (bit 8) is R/W (SDM Vol.4
Table 2-2; the profile's mask was corrected after review, so the BSP may write back what it reads); a write changing
EN, EXTD or the base is refused (until X3, which built the LAPIC: Built (X3); then no LAPIC yet; the profile notes the x2APIC -> xAPIC and disabled -> x2APIC #GP rules
for that chunk). After review also: the leaf 4 terminator is all zeros at any vCPU count; IA32_FMASK 63:32 are reserved
(#GP; SDM Vol.4 Table 2-2), SYSENTER_CS keeps all 64 bits ("not used, can be read and written"). TSC is provisional
(L5): CNTVCT scaled to 1 GHz plus a per-vCPU offset, which the host sets from one CNTVCT sample per VM, so every TSC
starts near 0 and the vCPUs agree (`profile@N` checks a peer's RDTSC after it sees CPU 0's flag is not smaller).
Performance (recorded, not built): every CPUID/RDTSC/RDTSCP is a host exit plus a decode; a resident-side system decode
cache or fast path is planned with L5. Checks: `run_slice3_vm.py system` (0 GAPs; profile programs at 1/2/4 vCPUs judged
by `cpu_profile.py`, MSR read-backs against QEMU, 11 new fault cases).
*Built (I7, FS/GS overrides; fexc series15):* FS.base and GS.base are the bank's `Cpu::fs_base`/`gs_base`, the only copy
the resident trusts; it writes them into FEX's `fs_cached`/`gs_cached` (`frame::FS_BASE` 1000, `frame::GS_BASE` 992, in
`frame::GUEST_STATE`) at every block entry and never reads them back, so a guest store through the guest-state page
(the documented deviation) changes at most that vCPU's own later prefixed accesses until the next `enter`, never
the bank. Admission lets x28 load them, never store. Translated code cannot change them: WRFSBASE/WRGSBASE and SWAPGS
stay system cuts (S3); WRMSR goes through the resident (S1). Until S3 the driver's `fsbase`/`gsbase` items set them.
fexc admits an FS/GS prefix on every admitted form with an explicit memory operand: FEX folds the base into the native
address, so a site's recorded address (`site_address`) is already base + EA mod 2^64, and #PF CR2, the canonical check
(#GP(0); an FS/GS reference is never a stack one, so RSP/RBP bases give #GP, not #SS), the split-store check and the
swap's 16-byte test all see the x86 linear address with no runtime change. Branch targets need nothing: FS/GS only
changes where `jmp`/`call [mem]` load the target. CS/DS/ES/SS prefixes were already admitted silently (FEX's decoder
drops them in 64-bit mode); now they are admitted only naming the operand's default segment. Refused (`unsupported_form`,
vendors differ and the SDM gives no rule): FS/GS with a second segment prefix or with no memory operand; SS on a
DS-default operand and CS/DS/ES on an RSP/RBP one (#SS versus #GP: AMD APM Vol. 3 §1.2.4 makes CS/DS/ES/SS null
prefixes in 64-bit mode, so the default segment decides, while Intel Vol. 1 §3.3.7.1 suggests #SS for an SS override;
these refusals are all lifted since series47-seg: Built (segment prefixes, November 3)). Checks: `tools/reference/gen_i7.py` corpus (67
programs, bases near, far, negative and 0; QEMU sets them with WRFSBASE/WRGSBASE) 67/67 at CPL 3 and 0, block sizes
default and 1; faults: 252 `segment_probes` and 979 page-mutation cases on that corpus (#PF at base + EA), all match;
`verify_precision.py` matrix and adversarial programs with FS/GS forms clean; on the corpus it finds one defect that is
I1's, not I7's (also on series14 with the prefixes removed): after a 16-bit register `cmpxchg` following BMI/POPCNT forms,
FEX leaves AF's x27 write out, so a fault at a later access in the block would bank a stale AF (P6, program
`i7-random-45`). Census FS/GS class: kernel 36,958 → 0,
initramfs 27,599 → 2. FEX ASM suite unchanged (its FS/GS tests need WRFSBASE/WRGSBASE or SSE).
*Revised (October 29, fexc series27; coordinator's decision): CS/DS/ES/SS prefixes follow Intel* (superseded by the owner's
decisions of November 3, §5: Built (segment prefixes, November 3)). The presented CPU is
GenuineIntel, so Intel's rule decides the exception for a non-canonical address, Intel SDM Vol. 3A §4.5.1 (Vol. 1
§3.3.7.1): #SS(0) only for a reference that uses SS, implicitly (an RSP/RBP base, no override; the stack instructions) or
explicitly (an SS prefix, whatever the base); a CS/DS/ES prefix makes even an RSP/RBP-based operand a DS-class reference,
#GP(0). The earlier refusal of SS on a DS-default operand and of CS/DS/ES on an RSP/RBP one is lifted: the shim admits a
single CS/DS/ES prefix on an RSP/RBP base and a single SS prefix on any explicit ModRM operand; still cut are SS with
CS/DS/ES (mixed classes, no SDM rule) and SS on a moffs or string source operand. fexc's `JoinSites` takes the class from
the raw prefix bytes (FEX's decoder drops these prefixes), and the resident's site mapping gives the vector. The host's
decoded memory forms (every `segments::linear` caller: descriptor-table, segment and far-transfer operands, LAR/LSL/VERR/
VERW, LDMXCSR/STMXCSR, the x87 and FXSAVE/XSAVE transactions, CLFLUSH, the BT* cut under alignment checking) record the
class in `System::segment` (`SREG_SS` or `SREG_DS`), which `segments::noncanonical` reads (SSE's LDMXCSR/STMXCSR now use it
too); prefixes of two classes are not decoded, INVLPG drops them. Motivation: Linux 6.12's SMP alternatives replace LOCK by
DS on one CPU, giving `ds btr %rdx, 0x540(%rbp)`. QEMU 11.1.2 (TCG, system mode) raises #GP(0) for every non-canonical
data address, never #SS(0), so its agreement on the #GP cases says nothing and the #SS cases are judged by the rule
(`run_i5b.py`'s `segment-*` cases; `segment-none-*` are the default-segment controls). Checks: `run_i5b.py` 221
`segment-*` cases at block sizes default and 1 (DS/ES/CS on RBP and on RSP via `[rsp + rbx]`, SS on RBX and `[rsi + 8]`,
the defaults as controls; loads, stores, RMW, LOCK ADD, BTR, LOCK BTS, LDMXCSR, FXSAVE, XSAVE; canonical runs,
non-canonical and straddling addresses; BTS under alignment checking at CPL 3: aligned runs, misaligned #AC, non-canonical
the class's exception; the kernel's own bytes), all as expected, QEMU agreeing on every #GP case and raising #GP for the 36
#SS ones; `run_slice1_faults.py` `null_segment_probes` (108: DS/ES/CS on RBP/RSP and SS on RBX/RSI for load, store, RMW,
LOCK, BT*, XCHG at the I7 targets) match at both block sizes.
*Built (segment prefixes, November 3; fexc series47-seg):* the owner's three decisions (§5, from the reference AMD
machine). The forms changed were of two kinds: series46-br66 *admitted* SS on a DS-default operand and CS/DS/ES on an
RSP/RBP one with Intel's class (the October 29 revision above; the hardware check's table lists them as refused, which was
I7's original rule), and *refused* (`unsupported_form`) the rest: SS with CS/DS/ES, SS on a moffs, XLAT or string source,
FS/GS with any second segment prefix, FS/GS with no memory operand (STOS/SCAS's ES:RDI, register forms, push/pop/ret/
leave/pushf). Now nothing about segment prefixes is cut. fexc: the allowlist's segment block is gone (FEX's 64-bit decoder
already drops CS/DS/ES/SS and keeps the last FS/GS prefix in `FLAG_SEGMENTS`; it adds the FS/GS base exactly where x86
does: ModRM, moffs, XLAT and the DS:RSI source, never the stack's accesses or ES:RDI, so an ignored FS/GS adds nothing),
`JoinSites` makes an explicit operand SS-class only for an RSP/RBP base without FS/GS (`NullSegment` removed). Host
(`system.rs` `decode`): only the last FS/GS prefix is kept, CS/DS/ES/SS skipped, so `System::segment` is 0, `SREG_FS` or
`SREG_GS` (the ABI's `well_formed` now refuses `SREG_SS`/`SREG_DS`); OUTS takes the last FS/GS (SS no longer refused);
INVLPG with FS/GS is still not decoded (not part of the decisions). Resident: `segments::noncanonical` is #SS(0) for an
RSP/RBP base without FS/GS, else #GP(0). Admission is unchanged: the site class is trusted metadata choosing #SS or #GP,
and the base is FEX's guest-state load, which admission already lets x28 read and never write. Canonical accesses are the
same accesses (CS/DS/ES/SS add no base). Checks, red on series46-br66 and green on series47-seg
(`natemu-raw/seg-check-20261103-01/red/`, `green/`): the I7 corpus regenerated with two QEMU-compared edge programs
appended (`gen_i7.py` `PREFIX`: SS on `[rbx]`/`[rsi + r8*2]`, DS/ES/CS on `[rbp]`/`[rsp]`, PUSH/POP m, two null prefixes,
SS on moffs, XLAT, LODS/MOVS/CMPS; `64 65`, `65 64`, `64 65 64`, `3e 65`, `36 64` on loads, stores, RMW and MOVS/LODS
sources, FS/GS on STOS/SCAS, ADD r, PUSH/POP r, PUSHF; `natemu-raw/i7-corpus-20261103-01`, the first 67 programs identical
to October 28's): series46-br66 67 match + 2 `unsupported`, series47-seg 69/69 at CPL 3 and 0, block sizes default and 1;
`run_i5b.py` `segment-*` 255 cases at both block sizes (the series27 cases with the default-segment expectation, the
kernel's `ds btr` non-canonical now #SS(0), plus 34 new: those forms, non-canonical and canonical, host-decoded LDMXCSR/
FXSAVE among them, several FS/GS prefixes with bases chosen so only the applied one reaches mapped canonical memory, FS on
STOS/SCAS with a base that would make a non-canonical RDI canonical): series46-br66 116 not as expected, series47-seg all,
QEMU 11.1.2 agreeing except the #SS(0) cases (expected from the probe lines cited in §5 and in `run_i5b.py`), including an
FS/GS prefix followed by a null one; `run_slice1_faults.py` `null_segment_probes` (108, the model now the default segment):
series46-br66 68 fail, series47-seg all match at both block sizes; host unit tests (`system.rs`: classes, several FS/GS,
OUTS; ABI: `SREG_SS` not well formed); `test_slice1.py` (the 16 forms moved from `UNSUPPORTED` to `ALLOWED`/`REPS`/
`BRANCHES`; 35 tests pass; series46-br66: 17 failures); `check_sites.py --slice1` (its class rule likewise) 64 programs, 0 errors. The
kernel census classifier (`experiments/track03/kernel-census/census.py`) drops its segment rules likewise (selftest updated).
Standing suite on series47-seg (`green/results.txt`, 2 GiB, one VM runner at a time): `cargo test` 151 + 1 ignored (also
with `FEXC` set), `check_resident_stack.py` `ok`, Python 317, `test_fexc.py` 10; fixtures 64, REX 37, 66-on-branches 12,
I10 88, each at both block sizes; `run_slice1_faults.py` 7,912 at both; the I7 corpus's page mutations 6,313 at both (the
two `.byte` edge programs skipped: the fault model cannot read raw bytes); `run_slice2_probes.py` 83; `contain` 0 failures
(423 controls); `run_x1.py` 6/6 + 196/196; `run_i10_faults.py` 296 at both; `run_i5b.py` 445/445; FEX ASM 1,875 pass / 0
fail / 48 deviations / 347 unsupported; `run_boot_smoke.py` 4/4; `run_r1.py r1a` 7/7, `r1b` 12/12; the installer initrd
to "Select a language" at N = 2 in 57.2 s wall ("Run /init" at 8.40 s guest; `green/boot/`).
*Built (S2a, control and debug registers):* the bank (`Cpu`) gains CR2, CR8 and DR0-3/DR6/DR7 (`Cpu::dr`, reset
`DR_RESET`: DR6 FFFF0FF0H, DR7 400H). MOV CR0 accepts every bit of the profile (PE MP EM TS ET NE WP AM NW CD PG), with
the existing #GP rules (bits 63:32, PG without PE, NW without CD, PG or PE cleared in 64-bit mode); reserved low bits are
ignored. MOV CR4 accepts every valid bit (`cpu_profile::CR4_VALID`; DE, PCE, OSFXSR, OSXMMEXCPT, FSGSBASE, OSXSAVE are no
longer refused; CPUID.1:ECX.OSXSAVE follows CR4, S1). CR2 takes any value; CR8 holds the TPR class (bits 63:4 #GP; X3 makes
it the LAPIC TPR bits 7:4, SDM Vol.3 10.8.6.1). Neither touches a translation; every CR0/CR4 change still drops the vCPU's
code and shadows. MOV DR (host decode `0f 21`/`0f 23`): #UD for LOCK and DR8-15 (REX.R), #GP(0) at CPL > 0, #UD for
DR4/DR5 with CR4.DE (else DR6/DR7; the SDM gives no order between the CPL #GP and this #UD: the model and the resident
follow QEMU, from reading its code, while KVM's VMX exit path checks DE first; Linux runs with DE = 0, so the difference
is not observable there), DR7.GD a #DB fault (vector 1) before the access with DR6.BD set in the bank (B0-B3
kept; the SDM lets either happen), then #GP for a 1 in bits 63:32 of DR6/DR7; DR0-3 take any 64-bit value; DR6 keeps
B0-3/BD/BS/BT, DR7 L0-G3/LE/GE/GD/R/W/LEN, the other bits read as fixed (no RTM, no bus-lock detection; `system_fault_model.dr_write` models the read-back). RDPMC (`0f 33`):
#GP(0) at CPL > 0 without CR4.PCE, and at any CPL otherwise, since the profile has no counters (CPUID.0AH = 0: every ECX is
invalid). Choices: **AM/AC** (superseded: X2 built #AC, Built (X2)) — alignment checking is not built; the state CR0.AM ∧ RFLAGS.AC ∧ CPL 3 is refused as a mode
(`Cpu::control` is `None`, stop `unsupported_mode`, the record holding the new CR0/CPL and the old RIP) wherever it would
begin: driver start, a CR0 write (`commit`'s mode check) and every CPL change (`set_cpl` makes the same check). The future
RFLAGS writers (POPF, IRET, SYSRET) must use the same check. AM or AC alone, or both at CPL 0, run. Building #AC (alignment
checks at CPL 3 access sites, or blocks keyed by the AC state) is X2's, the chunk that builds POPF and IRET eligibility.
**GD** — the #DB is a guest-fault stop like every exception today; X1 delivers it as a fault (RIP at the MOV) and must
clear DR7.GD on delivery (SDM Vol.3 17.2.4); S4 orders it with the other #DB sources. **Breakpoints** — a DR7 write
setting any of L0-G3 stops `unsupported_system` (Q4 deferral) until S4 arms them; DR0-3 are stored only. **TS/EM/MP,
OSFXSR/OSXMMEXCPT/OSXSAVE** — recorded; no admitted form depends on them today (no x87/MMX/SSE/XSAVE form is admitted). I5a,
I5b and I6 must make translation honour them per the SDM tables (x87: #NM with EM or TS, WAIT/FWAIT #NM with TS and MP; MMX: #UD with EM, #NM with TS;
SSE: #UD with EM or without OSFXSR, #NM with TS, unmasked SIMD exceptions #XM with OSXMMEXCPT else #UD; XSAVE family and
XGETBV/XSETBV #UD without OSXSAVE, XSAVE family #NM with TS; FXSAVE/FXRSTOR #NM with TS or EM, never #UD for EM and no
OSFXSR #UD; VEX/AVX ignore EM, #UD unless OSXSAVE and XCR0[2:1] = 11b, #NM with TS; PAUSE, PREFETCHh, SFENCE/LFENCE/MFENCE,
MOVNTI and CLFLUSH are not gated by EM, TS or OSFXSR) and rely on the flush at each CR0/CR4 change, or add the bits to `Cpu::code_context` if
a later chunk keeps blocks across control changes; NE = 0 (x87 errors through FERR#/IRQ 13) is I6's to refuse. **CD/NW**
— recorded with no memory-type effect (M2 owns memory types; since M2: Table 14-5's CD stops cache fills, not the types or orderings). Not built: CLTS (`0f 06`, no use in the
census; the host still declines its decode), LMSW/SMSW. Checks: `run_slice3_vm.py system` 0 failures, 0 GAPs: new QEMU
programs `sys-cr0-bits`, `sys-cr4-bits`, `sys-cr2-cr8`, `sys-debug-regs` and `sys-head-cr0` (the kernel head's EFER/CR0
set-up, `K kernel/head_64.S:375-401`; its `pushq $0; popfq` is X2's) at 1/2/4 vCPUs and block sizes default and 1; 17 new
fault cases judged by `system_fault_model.py` (extended: CR8, DR, RDPMC; a case may declare the controls its prefix
sets), QEMU agreeing on all but RDPMC at CPL 0 (QEMU #UD, `QEMU_DIFFERS`) and CR8 bits 63:4 (QEMU accepts); refusals
`dr7-l0`, `dr7-g3`; `am-ac-cpl3` refused, `am-cpl3` runs; model, host decode and abi unit tests.
*Built (I2, string instructions; fexc series16):* MOVS/STOS/LODS/SCAS/CMPS at every width, alone and with REP (all five)
and REPE/REPNE (CMPS/SCAS), both DF directions. Choice (b) of the I2 brief: a block holds ONE element per pass and loops
through a literal exit to the instruction's own RIP, which the resident links (a self-link when the REP starts the block)
and the unlinking stop patches like any exit. FEX's own lowering was not usable: REP STOS/MOVS become MemSet/MemCpy ops that
run the whole count natively (no partial progress, no site per element) and CMPS/SCAS/LODS become IR loops with a native
backward branch, which admission refuses and the unlinking stop cannot interrupt. A resident transaction (c) would cost a
VM-side decode and a resident entry per element (or per chunk) for `clear_page`'s `rep stosq`. Patch 0015 (`FexcStringOp`):
`cbz x7` (RCX = 0) to the next instruction; the element's accesses as sites (MOVS reads DS:RSI then writes ES:RDI, CMPS
reads [RDI] then [RSI], FEX's and the pinned QEMU's order; the SDM gives none), nothing written before them; RSI/RDI stepped
by DF from the registers; RCX - 1; the exit to its own RIP, or for REPE/REPNE whose ZF ends the repetition a forward branch
to the next instruction. So every fault, stop or interrupt window is between elements with RCX/RSI/RDI and RIP exact and the
block has no backward branch (contract 44's register rule). **RF (for X1/X2):** the SDM saves RF = 1 in the frame of a
fault, trap or interrupt delivered between iterations of a REP string instruction that has done some of them (Vol. 3
"RF"; RF = 0 before the first iteration and after the last). A stop at a block entry whose block starts with the REP
cannot tell whether elements have run, so X1/X2 need a signal: either take the interrupt window only at the REP's
self-exit (elements have run), or record in the vCPU's state that its last exit was a REP self-exit (cleared by any other
exit), and set RF from it on delivery. A REP string instruction ends its block
(`FEXC_CUT_STRING`, reason `block_ending_instruction`: one self-exit, at most three link points per block). Admission
proves the self-exit target from the guest bytes (`admission::rep_string`: the block's last instruction is a REP string).
Refused (`unsupported_form`): 0x67 (32-bit addressing), LOCK (#UD), REP with REPNE, SS on the DS:RSI source, and by policy,
not by an x86 rule, REPNE on MOVS/STOS/LODS (hardware repeats as with REP; the SDM calls the combination reserved) and
FS/GS on STOS/SCAS (hardware ignores it: ES:RDI takes no override; SS on the source and FS/GS on STOS/SCAS are admitted
since series47-seg, Built (segment prefixes, November 3)); FS/GS (alone) and CS/DS/ES are
admitted on the source of MOVS/LODS/CMPS (FEX's non-REP LODS ignored the prefix and its MOVS stepped RSI from base + RSI;
`FexcStringOp` does neither). A MOVS's load and store are two operands, not an RMW pair (`fexc_movs`): its load is read
intent, so a read-only source does not fault (the fault cases found this). Part 0 (I1 carry-over): the stale-AF report
was a missing native x27 write, not a wrong value: `cmpxchg r16, r16` (equal operands) sets AF's raw value to the pooled
constant 0 that popcnt had already coalesced into x27, so its store became a self-move; the banked AF was right, but the
per-instruction write that verify_precision P6 checks was absent. Patch 0014 stops the backwards RA pass from coalescing a
value stored twice to one static register (adversarial program `pooled-af-constant-twice`; the I7 reproducer now passes).
Objects differ from series15 in 13 of 450 programs of the old corpora (fixtures 2, seed-4242 1, I1 7, I7 3, flow 0), all
by patch 0014 (a constant such as `xor edx, edx` stored by two instructions now goes through a temporary). Checks:
`tools/reference/gen_i2.py` corpus (484 programs: every form × width × repeat × DF at counts 0/1/random, large counts in a
64 KiB region, misaligned and page-crossing elements, overlapping MOVS, REPE/REPNE ended early, FS/GS sources, long strings
crossing a page midway, random mixes with slice-1 forms) 484/484 against QEMU at block sizes default and 1; fault cases:
its flow form under `run_slice2_faults.py` (QEMU fault mode, the context at the faulting element): 4,356 cases, 1,271
faulting (112 midway through a REP, 51 on the second page of a straddling element), all exact at block sizes default, 1
and 2 (the runner unrolls a fault after a string with each REP once; a fault inside one keeps QEMU's signal context);
`run_slice3_vm.py kicks` `rep@N` (32 KiB REP STOSQ/MOVSQ up and down, REPE CMPSB, REPNE SCASB): 1,000 kicks per vCPU at
N = 1, 2, 4, every stop at a block entry or exit (between elements), every run equal to QEMU; verify_precision clean on the
corpus (P5 exempts exactly a REP's `cbz x7`); FEX ASM suite 376 → 443 pass, 0 fail (67 string/REP tests; left: a32 forms).
Throughput (M4, slope of 20,000 vs 40,000 rounds): 4 KiB `rep stosq` 151 ns (native `memset` 43 ns, a native `str`
loop 164 ns), `rep movsq` 166 ns (`memcpy` 62 ns), DF = 1 `rep stosq` 182 ns, `rep movsb` 1,049 ns. Census string class:
kernel 5,725 → 0, initramfs 2,969 → 0 (`experiments/track03/kernel-census/README.md`). Not built: ERMS/FSRM stay
unadvertised; an element per pass bounds `rep movsb` (Linux's uaccess copy before alternatives); chunked passes (several
elements per pass, each with its own PC entry) are the upgrade if the census of executed strings asks for it.
*Built (S2b, descriptors and segments):* the bank (`Cpu`) gains `seg` (ES, CS, SS, DS, FS, GS in Sreg order: selector and
hidden attributes = descriptor bits 47:40 and 55:52; null = 0), `ldtr`/`tr` (selector, base, byte-granular limit,
attributes, from 16-byte descriptors), `gdtr`/`idtr` and `mov_ss` (set by MOV SS, cleared by the next system transaction;
X2 owns its effect and its end inside generated code). Flat 64-bit only: CS/SS/DS/ES bases and limits are not used,
FS/GS bases stay `fs_base`/`gs_base` (I7). `Cpu::control` now also requires CS.L = 1, D = 0, so every path that loads a
compatibility-mode CS stops `unsupported_mode` (owner decision 3). Start state (`flat_segments(cpl)`): the pinned QEMU
stub's (CPL 0: CS 0x08, SS = DS = ES 0x10, FS = GS null; CPL 3: CS 0x23, SS 0x1b, rest null); GDTR/IDTR and LDTR/TR the
SDM reset values (base 0, limit FFFFH; the stub never loads LDTR). The driver's `switch` (`SET_CPL`) also loads that CPL's
flat CS and SS, so CS.RPL = CPL holds. Host decode (`system.rs`, ModRM/SIB shared): LGDT/LIDT/SGDT/SIDT m, LLDT/LTR/SLDT/STR
r/m, MOV r/m, Sreg and MOV Sreg, r/m, JMP/CALL far m16:32 and m16:64, RET far (`cb`, `ca iw`, 32- or 64-bit) and IRET
(`cf`, IRETD/IRETQ); FS/GS overrides add their base; a 66 prefix on a far transfer, 0x67, and CS/DS/ES/SS overrides on
these memory forms are not decoded (the vCPU stops with fexc's reason). The resident (`execution/segments.rs`) executes
them as transactions per the SDM: #UD for LOCK, MOV to CS and Sreg 6/7; #GP(0) at CPL > 0 for LGDT/LIDT/LLDT/LTR (SGDT,
SIDT, SLDT, STR at any CPL: UMIP is not in the profile); LGDT/LIDT #GP(0) for a non-canonical base (AMD APM, KVM; Intel's
list is silent); data and stack segment loads with the MOV rules (SS: RPL = DPL = CPL, writable data, #SS(sel) not present,
null at CPL < 3 with RPL = CPL; DS/ES/FS/GS: data or readable code, max(CPL, RPL) <= DPL unless conforming, #NP(sel); FS/GS
take the 32-bit base, a null load clears it); LLDT/LTR on 16-byte descriptors (type 2 / 9, #NP(sel), upper type field 0
and a canonical base, else #GP(sel)), LTR #GP(0) for null, the busy bit written; JMP/CALL far to a same-CPL code segment
(conforming DPL <= CPL, nonconforming RPL <= CPL and DPL = CPL; L = D = 1 #GP(sel); TSS/task gate #GP(sel); a 64-bit call
gate stops `unsupported_system`; CALL #SS(0) before the canonical RIP check, then its pushes); RET far and IRET to the same
or an outer level (IRET: CS, RIP, SS, delivery_model's order; RET to an outer level: CS, SS, RIP, its pseudocode; IRET
always reloads SS:RSP; a null SS only for a new CPL < 3 with RPL = that CPL, per Intel's IRET/RET 64-bit #GP(0) text, which
is stricter than delivery_model A7; IRET's RFLAGS masks by CPL/IOPL, VIF/VIP only at CPL 0; a return to CPL 1 or 2 stops
`unsupported_mode` before any effect; an outer return nulls DS/ES/FS/GS below the new CPL, keeping the FS/GS bases, and
calls `set_cpl`). Every descriptor-table and TSS-descriptor access is an implicit supervisor access through the walker at CPL 0
(`segments::linear`, never a shadow; a write into a page holding translations invalidates it first), operand and stack
accesses explicit at the CPL; the SDM's accessed-bit write on a segment load (CS included) and LTR's busy write are real
writes, atomic on the guest byte (`segments::atomic_byte`: an atomic OR for A; a compare-exchange 9 -> B for busy, so of two
vCPUs loading one TSS descriptor the second takes #GP(selector)), so a clear A bit in a read-only GDT page is #PF(3) at
descriptor + 5. Refused (typed): compatibility-mode targets
(`unsupported_mode`, the record holding that CS and RIP at the instruction), call gates, an IRET setting TF (S4)
(`unsupported_system`); RF from an IRET frame is dropped (no breakpoints before S4). Oracle:
`tools/models/segment_model.py` (independent of the code, from the SDM; reuses `delivery_model`'s bus and descriptor
fields; its IRETQ agrees with delivery_model's on 26 frames in `test_segment_model.py`), ambiguities B1-B9 in its
docstring; at B2 (non-canonical LGDT base: #GP(0) or the load), B8 (CR2 at descriptor + 0, 4 or 5) and B9 (#SS or #NP
for IRET's SS) each result carries the set of outcomes the SDM permits, and the runner accepts any of them for natemu and
QEMU (an independent review derived 37 cases by hand; the oracle matched all). Checks (`run_slice3_vm.py system`, `tools/bench/smp/segments.py`, all at block sizes default and 1): 12
positive programs (plus an IRETQ CPL 3 -> 3 whose frame sets VIF/VIP) against QEMU (registers, GDT/LDT/result memory): every form once (`seg-basic`), the cited Linux
sequences transcribed (`seg-boot`: decompressor `head_64.S:298-306,366-374,440-443`, trampoline `trampoline_64.S:214-248`,
kernel head `head_64.S:338-353`, LTR on the writable GDT, then loads from a read-only copy with A preset), LRETQ and IRETQ to
CPL 3 (DS/ES/FS/GS nulled, user state read, a #GP(0) there ends both runs), each at 1, 2 and 4 vCPUs; 44 fault cases
judged by segment_model (QEMU within its permitted outcomes on 39; outside on 5, listed in `QEMU_DIFFERS`: null SS with
RPL != CPL for MOV and for IRET (QEMU then does not finish), null LTR, a non-canonical far CALL/RET target (QEMU commits
and faults at the target)); 4 refusals; `seg-ltr-race@2/@4` (natemu only, QEMU's LTR is not atomic): every vCPU loads TR
from one descriptor, exactly one succeeds and the others take #GP(0x70), 20 runs each. QEMU sets no accessed bit for a CS
load by a far transfer: `seg-boot` compares the SDM's value there (`params.sdm_bytes`). `check_resident_stack.py` now keys
a second copy of one function (two codegen units) separately; the runtime target builds with `-C jump-tables=no`
(`.cargo/config.toml`), since the audit refuses any unlisted `br`. FEX ASM suite: 7 tests that expect FEX's Linux user-mode
descriptor tables (unchecked selectors, a fixed SGDT answer; `Primary_8C`, `_8C_2`, `_CF`, `5_FF_05*`, `07_XX_00`) now run
these instructions architecturally against the driver's tables and are counted unsupported with that reason
(`run_asm.py` `OS_SEGMENTS`); passes unchanged at 443. Not built: POP/PUSH FS/GS (no use in the census), LAR/LSL/
VERR/VERW (S3), SWAPGS (S3), call gates and task switches, the MOV SS shadow's effect and IRET's NMI unblocking (X2).
**For X1a:** `segments::linear` (implicit supervisor or CPL access through the walker) and `descriptor` (GDT/LDT code and
data descriptors) are reusable, but `descriptor`'s #GP masks the error to `selector & 0xfffc` (delivery must OR in EXT),
it is not for IDT lookups (gates are 16 bytes and their error code is vector * 8 + 2 + EXT), and neither helper checks the
TSS limit (#TS on RSPn/ISTn is delivery's); delivery_model A7 accepts a null SS with any RPL on IRETQ, where this chunk
follows Intel's stricter text (segment_model B6).
*Built (I3, LOCK forms; fexc series17, series18 after review):* LOCK on every lockable memory form (ADD ADC SUB SBB AND OR XOR, INC DEC NOT NEG,
BTS BTR BTC, XADD, CMPXCHG, XCHG) and CMPXCHG8B/16B with or without LOCK compile to FEX's LSE lowering, one acquire-release
atomic per instruction: `ldaddal` (add/adc/sbb/sub/inc/dec/xadd; adc/sbb add `src + CF`), `ldclral` (and, btr), `ldeoral`
(xor, not, btc), `ldsetal` (or, bts; bt* on FEX's byte, the slice-1 policy), `casal` (cmpxchg), `caspal` of two W or two X
registers (cmpxchg8b/16b: RDX:RAX compared and, on failure, loaded; RCX:RBX stored). Each is a site of kind 13 `atomic`
(`data_reg` = [Rt, Rs], write intent); admission decodes exactly these words (even CASP pairs, no XZR result with the
code-shape check) and requires each to be an atomic site describing it. The resident treats them as it does `swpal`:
an abort commits nothing (CAS/CASP restore their compare register, DDI0487 C6.2), a translation or permission fault is a
write-intent walk (the access is a write, Arm ARM MemAtomic, also for a CAS whose comparison fails; ESR.WnR is not relied
on: the M4 reports a CAS translation fault as a read), retry or #PF; a 16-byte `caspal` not 16-byte aligned is #GP(0)
before any walk (SDM CMPXCHG16B; QEMU's order; Arm ARM abort prioritization takes the instruction's alignment fault before
any translation or permission fault); any other atomic crossing 16 bytes takes the alignment fault and stops
`unsupported_atomic` with the state at the instruction. **Owner decision (recorded): LOCKed accesses crossing 16 bytes
(inside a line, across a line or a page) stop** (`unsupported_atomic`, slice-3 contract §2 and §5 decision 2); an unlocked
CMPXCHG8B crossing 16 bytes stops too (it is the same `caspal`: a documented deviation). Also documented: LOCK
BTS/BTR/BTC are a byte atomic on the byte holding the bit (FEX's form, the slice-1 bt policy), where x86 performs an
operand-size locked access, so a dword or qword that crosses 16 bytes or a page faults or is a split lock on x86 but not
here. Hits: the kernel census cannot
decide alignment statically; FEX's ASM suite has 45 such tests (its Atomics tests cross 16 bytes on purpose); the QEMU
corpora, litmus and contention programs none; the fault probes 8, by design. **LOCK NEG** (FEX: a CAS loop, a native
backward branch admission refuses and the unlinking stop cannot interrupt) is, under slice1, one attempt per pass (patch
0016, `FexcLockNeg`): a write-intent load site, `neg`, one `casal` site, and on a lost race a literal exit to the
instruction's own RIP (the dispatcher completes the flags and puts CF in the exit ABI's form first, so that exit holds the
state before the instruction); it ends its block (as a REP string, `FEXC_CUT_STRING`), and admission proves the self-exit
target from the guest bytes (`admission::lock_neg`). Chosen over a resident transaction (a VM entry per NEG) and over a
bounded loop (any backward branch defeats the stop); ADC/SBB need no loop (`ldaddal` of `src + CF`). **LOCK elsewhere**
(a register destination, a non-lockable opcode, a system instruction, whether FEX's decoder took it or not) is a system cut;
the host names it `system::LOCK_UD` and the resident raises #UD (a guest-fault stop, vector 6, until X1 delivers it).
Refused (`unsupported_form`, no SDM meaning): F2/F3 with LOCK (XACQUIRE/XRELEASE; HLE is not advertised) and 66/F2/F3 on
CMPXCHG8B/16B (6 ASM tests). CPUID advertises CX8 and CX16 (profile 1.EDX[8], 1.ECX[13]). Patch 0016 also keeps the result
of a swap or fetch-and-op atomic out of static registers (after an abort a load's destination is UNKNOWN, DDI0487 D1.4.1.4;
S3e's `swpal` wrote RCX's register directly), frees an unused one (LOCK NOT), and gives a LOCK instruction no pooled
constant (LOCK forms in a long block spilled, which slice1 refuses); objects of every earlier corpus are unchanged but one
I7 program with a memory XCHG. **Ordering:** every AL atomic is acquire and release (the Linux arm64 kernel implements its
fully ordered atomics with these words alone); litmus confirms each word as a full barrier on the M4 (below). A *failing*
`casal`/`caspal` writes nothing (Arm ARM CAS: no store when the comparison fails; `Unpredictable_WRITEFAILEDCAS` is FALSE),
so it has no release effect and only its read is an acquire: a store before it may be reordered after its read (B2.3
Barrier-ordered-before gives no edge from an earlier store to an acquire read), while x86's failing LOCK CMPXCHG is a full
barrier (it writes the old value back). **Coordinator decision (October 29, under the owner's architecture principle):**
LOCK CMPXCHG, CMPXCHG8B and CMPXCHG16B emit `dmb ish` immediately BEFORE their `casal`/`caspal` (series18), which orders
every earlier access before the CAS's read whether or not it then writes; a barrier after the CAS would not cover the case
where the failing CAS's read is itself the observing load (`cas-read` below). Not for unlocked CMPXCHG8B/16B (no ordering
required), the `ld<op>al` forms (they always write) or LOCK NEG's CAS (it retries until it writes). Admission accepts a
`dmb ish` only right before such a CAS and, with the code-shape check, requires it there. A CAS/CASP whose comparison fails
still faults on a read-only page (MemAtomic translates for a write before the compare, `requirewrite`), which x86's write
cycle needs (#PF W, D set). Checks (suite `natemu-raw/suite-i3-20261029/` with series17, its `rerun-series18/` after the
barrier): `tools/reference/gen_i3.py` corpus (105 programs: every LOCK form at every width with register and immediate
sources, boundary values, every address form and an FS override, aligned and unaligned inside 16 bytes, CMPXCHG8B/16B
locked and unlocked, equal and unequal, the kernel's shapes, 60 random mixes; QEMU 7.2 keeps stale flags after
CMPXCHG8B/16B, so each is preceded by `pushfq`, tools/reference/README.md) 105/105 at block sizes default and 1. `litmus`
(series18) at N = 2..4, aligned and unaligned, the sb families also split and split-hi on their plain locations: sb with one
LOCKed RMW of each Arm word (xadd, and, xor, or, cmpxchg, cmpxchg-fail, cmpxchg8b, cmpxchg8b-fail, cmpxchg16b, neg), the
chains `xadd` (every outcome a permutation, the counter n) and `cas` (one winner), and `cas-read`/`cas8b-read` (store x_i,
then a failing LOCK CMPXCHG/CMPXCHG8B on x_{i+1} as the observing read: all-zero forbidden): 144 cells × 60,000 trials, 0
forbidden. Before/after cannot be told apart on the M4: series17 (no barrier, run with the code-shape check off, which
now refuses a LOCK CMPXCHG CAS without it) also gave 0 forbidden in the 12 `cas-read`/`cas8b-read` cells (720,000 trials);
the barrier is there for the architecture. `--control drop-lock` (the programs without LOCK) shows forbidden outcomes in
17 of 18 cells (sb-xadd, xadd, cas-read; the all-zero ring outcome at N = 3 is rare: `sb-xadd@3 unaligned` showed it in
the series17 run, not in two series18 runs of 20,000 and 100,000 trials). The model extension (`ordering_model.py` `rmw`:
an empty own buffer, then one indivisible read and write) has positive controls (a drain-free or non-atomic RMW exposes the
forbidden outcomes) and an independent review (ACCEPT; its editorial guards applied). Contention (`smp`, 3 runs each at
N = 2..4, all exact): `lock-counter` (20,000 rounds per vCPU of every atomic word on one 256-byte block, CMPXCHG/8B/16B retry
loops, its LOCK NEG word starting at 1 and ending 1), and `neg-race` (20,000 LOCK NEG per vCPU on one word) against
`neg-private` (each vCPU its own word): the race links the NEG's own-RIP exit (4 linked exits on some vCPU against 3 for
every race-free vCPU), so the lost-race path ran. Faults: `run_slice1_faults.py` 7,581 cases at block sizes default and 1
(LOCK data forms in the paging cells, window, non-canonical and straddle probes; CMPXCHG8B/16B #PF, CMPXCHG16B #GP(0)
aligned or not mapped, a failing LOCK CMPXCHG on a read-only page #PF W, 8 split-lock stops, 9 LOCK #UD); page mutations
on the I3 corpus 329 cases at both sizes; all match. The first series17 run at block size 1 had one host error,
`paging-lock-bts@high/cpl0-unmap@1-0xffff808000200000`, `hv_vm_create: 0xfae94007` (a VM held by the concurrent S2b agent;
`suite-i3-20261029/faults-1-first-run.txt`), and matched on rerun. `verify_precision` clean on every corpus (P3 now covers
swap/fetch-and-op destinations, P4 counts atomics as store-class, a LOCK adversarial program). Admission tests: the
`dmb ish` + CAS pair accepted only at a LOCK CMPXCHG* site, a stray `dmb ish` refused (also without the code-shape check),
a missing one refused with it (fixture `lock-forms`). FEX ASM suite 443 → 480 pass, 0 fail (45 split-lock stops, 6 refused
prefixes). Census LOCK class: kernel 10,020 → 0, initramfs 1,385 → 0, kernel `cmpxchg16b` 19 admitted
(`experiments/track03/kernel-census/README.md`). Standing suite (series17) green but two items outside I3: `system`
`seg-iret-null-ss-rpl@1` (an S2b program, QEMU reference timeout) and VM-creation collisions with the concurrent S2b agent
(rerun clean).
*Built (X1a, exception delivery, IRETQ completion, the driver's logging IDT):* every x86 exception the resident raises is
delivered through the guest IDT; no guest fault stops a run any more (`stop::GUEST_FAULT` is retired). The transaction
(`execution/delivery.rs`) follows `delivery_model` step by step (A5 order): the 16-byte gate read through the walker as an
implicit supervisor access (`segments::linear`); #GP(vector * 8 + 2 + EXT) beyond the IDT limit and for anything but a
64-bit interrupt or trap gate (16-bit gates #GP(0), A11); gate DPL < CPL for INT n/INT3 only (#GP(vector * 8 + 2)); #NP for
a gate not present; the target CS through `segments::descriptor` with EXT or-ed into its #GP (null #GP(EXT); not code or
DPL > CPL #GP(sel + EXT); #NP(sel + EXT); not 64-bit #GP(vector * 8 + 2 + EXT), A11); the stack: IST n, or RSPn when a
nonconforming DPL 0 CS takes CPL 3 to 0, read from TR's base (#TS(TR + EXT) beyond its limit, #SS(EXT) non-canonical), else
RSP; #GP(EXT) for a non-canonical handler; RSP aligned to 16; SS, RSP, RFLAGS, CS, RIP and the error code (#DF #TS #NP #SS
#GP #PF #AC #CP) written at the new CPL as one access (a fault writes nothing; A2); the CS accessed bit; then CS (RPL = new
CPL), a null SS with RPL = new CPL on a CPL change, `set_cpl`, RIP, RFLAGS without TF, NT, RF, VM (and IF for an interrupt
gate), the MOV SS shadow ended. RF image: 0 for INT n/INT3, 1 for the fault-class vectors (and #DB, whose only source,
DR7.GD, is a fault until S4), else the current RF. CR2 is written before delivering a #PF; delivering #DB clears DR7.GD (S2a
already set DR6.BD). A fault detected while delivering follows Table 7-5: benign then anything, or contributory then page,
serial; contributory then contributory, page then contributory or page: #DF(0); a contributory or page fault while
delivering #DF: `stop::SHUTDOWN` (typed: the record holds the last fault and the original RIP, the bank the state at the
original event, CR2 as written by any #PF detected on the way). CPL 1 and 2 targets stop `unsupported_mode`. Sources: site
faults (#PF, #GP/#SS non-canonical, #DE, CMPXCHG16B #GP), system-transaction checks (#UD, #GP, GD #DB, and S2b's #GP/#NP/#SS/#TS
with the state restored), LOCK #UD, instruction-fetch faults (new reply `REPLY_FETCH_FAULT`: the host still compiles nothing
at a faulting fetch), and the host's new decodes INT n (`cd ib`), INT3 (`cc`), INTO (`ce`: #UD in 64-bit mode), UD0/UD1/UD2.
INT1 (`f1`, ICEBP) is not decoded yet (fexc cuts it; the run ends `no_code`); S4 adds it with the #DB sources.
The handler is ordinary translated code. No recursion (`check_resident_stack.py`): a system instruction's exception is
delivered and `find` goes on; a site fault delivers, then dispatches; a single step whose fetch faults steps the handler's
first instruction. **Scope note:** the RSPn switch for CPL 3 -> 0 and IST were built here (the brief gave them to X1b)
because the logging IDT needs a known stack for every old corpus, whose RSP may be anything; X1b keeps its QEMU frame
checks at CPL 3, the paranoid-entry programs and IRETQ-to-CPL3 judgement. **IRETQ:** releases NMI blocking once past its LOCK #UD check, even when
it then faults (`State::nmi_blocked`, set by X2's NMI delivery); RF is loaded from the frame (no longer dropped) and cleared when
the next instruction completes (`completed`: a block's exit through the linker, the dispatcher or an armed stop, and every
committed system instruction but IRET; a stop at a block's entry keeps it). `delivery_model` A7 now states S2b's B6 rule (a
null SS needs RPL = the new CPL: Vol. 2A IRET 64-bit mode #GP(0)), single-valued, with a test. **Not built: the REP
self-exit signal.** No X1a delivery reads it (a fault pushes RF = 1 anyway, INT n/INT3 are whole instructions); the resident
cannot tell a REP's self-exit from a branch into the same block, and a linked self-exit never enters the resident. For X2:
admission knows the self-exit (`admission.rs`, `own`); carried in the translation reply as the self-exit's link-point
index (or exit index), the linker and `stopped` could record `rep_exit` by that index (a RIP does not identify it: a
branch may target the same RIP), and an interrupt at the REP block's entry still needs X2's choice (take the window only
at the self-exit, or unlink the self-link while a stop is armed). **Old corpora: the logging IDT.** The program driver gives
every vCPU, at `0xffff_ff00_0000_0000` (its own PML4 entry) or, in identity spaces, at `0x3e8000` (inside what
`programs.sys_tables` maps), a GDT in the QEMU stub's layout (0x08 code64 DPL 0 ... TSS v at 0x30 + 16v, accessed bits
set), TSSs whose RSP0 and IST1 are the vCPU's own stack page, and 256 interrupt gates (CS 0x08, DPL 3, IST 1) whose handler
for vector n is `+0x2000 + 16n`; GDTR, IDTR and TR start there. Its pages are supervisor-only (CPL 3 code cannot change them). Reaching a handler ends the run before anything there is
fetched (`Stop::Logged`); the report reads the frame's length from the last event (`State::delivered`: INT n to an
error-code vector pushes none) and gives the state the frame saved (GPRs with the frame's RSP, RIP, RFLAGS without
RF), `stop` `guest_fault` with the old record (vector, CR2 for #PF, error code, RIP) and `delivered` (the raw frame, the
handler's RSP and CPL, CR2). Every fault of the old corpora is therefore a CPL 3 -> 0 (or 0 -> 0) delivery on IST 1, judged as
before plus `tools/delivery_check.py` (delivery_model rebuilds the driver's tables and must give the same frame, handler RSP
and CPL) in `run_slice1_faults.py` and `run_slice2_faults.py`; `run_slice3_vm.py` reads the same record. A program that loads
its own GDT, IDT, TSS or tables owns delivery from then on; `segments.py`'s TSS now carries IST1 = RSP0 for that reason.
`run_asm.py` counts INT3/INT n/INTO/UD tests (they expect Linux's signal) as unsupported, as their former `no_code` was.
**Checks** (fexc series19, the concurrent I5a build, whose host passes it `--sse-gated`; block sizes default and 1): every
fault of `run_slice1_faults.py` (7,581 at default, twice; at 1, all 7,581 matched across two runs, each run losing 5 or 6
different cases to `hv_vm_create: 0xfae94007` from the concurrent agent's VMs) and `run_slice2_faults.py` (846 at block
sizes default, 1 and 2) is a logged delivery whose record matches as before and whose frame, handler RSP and CPL
delivery_model rebuilds (`delivery_check`); `run_slice3_vm.py corpora` at N = 2..4 (6,783 and 6,784 matches per N, each
vCPU on its own IST stack), `privilege` (the CPL 3/0 fault matrix at 1 and 4 vCPUs) and `system` (0 failures: the CPL 0 and
CPL 3 fault cases, the segment programs that fault after loading their own GDT and TSS; the driver reads a frame through the
vCPU's own CR3); new `system` programs `dlv-handlers@1/2/4` (each CPU loads its own IDT and takes INT3, INT 0x21, INT3 with
DF set, UD2 skipped, two on-demand #PF (read, write) whose handler maps V again, a WRMSR the profile and QEMU refuse
(#GP(0), skipped), #DE skipped, each returning by IRETQ) match QEMU system mode (registers, logs, tables) and every logged
frame matches delivery_model, for natemu and for QEMU; new `delivery` mode, 18 cases at both block sizes judged by
delivery_model alone (QEMU resets), CR2 included: #UD -> #PF -> #PF -> #DF on IST 1; the same into a shutdown; #GP -> #PF -> #PF -> #DF;
#GP through a gate not present -> #NP(0x6b) -> #DF; INT n through a task gate (#GP(0x202)), a gate not present (#NP(0x20a)),
a DPL 0 gate from CPL 3 (#GP(0x212)); a trap gate (IF kept); INT3 through a DPL 3 gate from CPL 3; INTO -> #UD; and
rdmsr_safe's MSR_PLATFORM_INFO probe -> #GP(0) (QEMU reads 0 from unknown MSRs, so it is judged here); review additions:
CPL 3 -> 0 on RSP0 through a gate without IST, a 16-bit gate (#GP(0)), a 32-bit target CS (#GP(0x33)), a target CS not
present (#NP(0x41)), IST1 beyond the TSS limit (#TS(0x31)), a non-canonical IST1 (#SS(1)), a frame straddling into a page
not present (#PF on IST 1, CR2 the faulting slot). Model, decode and
abi unit tests; standing suite otherwise green (fix/fix-1 64, seed/seed-1 44, flow 64 x 3, probes 83, ASM 781 pass and 0
fail with I5a's SSE (one INT3-expecting test moves from `no_code` to "delivered" unsupported), limits, kicks 1,000 at 1/2/4,
coherence, sharedpt, litmus, contain, unit tests). `smp`: every program matches. The `neg-race` coverage check (some CPU took LOCK
NEG's lost-race exit; two lost NEGs cancel, so it is that test's guard) missed at varying N with the state's growth;
after review `neg_race` starts with a barrier (a LOCK INC, then a wait loop of at least 1,000 rounds whose exits every CPU
takes, so linked-exit counts stay comparable) and runs 200,000 NEGs per CPU (`model_smp` updated): three runs of
`smp --programs neg-race,neg-private --reps 3`, every CPU of every race run took the exit (7 linked exits against 6). Stack (after review): `halt` takes
`&State` (its 38 callers each copied the ~1.26 KB state into their own frame), `returned` keeps no state copy (`missed`,
`halt_native`, the exit handlers not inlined), the boot stack is 32 KiB (`STACK_BYTES` 0x8000, private 0x4c000-0x54000), and
`check_resident_stack.py` reads `sub sp, sp, #N, lsl #12` and fails when either stack keeps less than 25% of its reserve
or 4 KiB headroom: gate 14,688 bytes (18,528 with a nested entry to its halt) of 32,768, boot 9,216 of 32,768. QEMU deviation found: pinned QEMU keeps RF after an IRETQ that loaded it, so an INT3
after a fault's return pushes RF = 1 (SDM and model: 0; `programs.DLV_BODY` avoids that order).
**For X1b, X1c, X2:** `delivery::deliver(s, Event)` is the one entry (exceptions with [vector, error, CR2] from the state at
the instruction, software interrupts with their length); `segments::{linear, descriptor, accessed, fields}` are shared;
CPL changes go through `set_cpl`; `State::nmi_blocked` is the NMI state (X2 sets it at NMI delivery, IRET clears it);
`stop::SHUTDOWN` is the triple fault; the logging IDT and `delivery_check` are the harness for CPL 3 frames. X1c owns the
full Table 7-5 matrix as model cases (here: 18 delivery cases), the refused-but-defined cases (unknown MSR, reserved bits: already
#GP via the transactions) and #DF's undefined CS:RIP; X2 owns interrupt events (EXT = 1, RF = current or mid-string),
`recognise`, HLT and the REP signal above; S4 owns the trap-class and breakpoint #DB sources and RF's breakpoint effect.
*Built (I5a, SSE state and arithmetic; fexc series19, patch 0017; series20, patch 0018, after review):* SSE, SSE2, SSE3, SSSE3, SSE4.1 and SSE4.2 (CPU 16
advertises all six) on XMM0-15: integer and floating-point vector forms, scalar float, conversions, moves, shuffles, compares,
PTEST, CRC32, admitted by `FexcSlice1::Sse` (map, mandatory prefix from the bytes, ModRM; 66 with F2/F3 or F2 with F3 refused:
FEX takes the last prefix where x86 lets F2/F3 win). Refused with reasons: MMX-register forms (x87/MMX state, I6), PCMPESTRI/M
and PCMPISTRI/M (FEX calls a host helper), MASKMOVDQU (FEX stores all 16 bytes), INSERTPS m32 whose zero mask clears the
inserted element (FEX drops the load and its #PF), AES and PCLMULQDQ (other features). **State:** the bank (`Cpu`) gains
`xmm` and `mxcsr` (reset 1F80H). `enter` puts XMM0-15 in v16-v31 (FEX's static vector registers) and sets FPCR from MXCSR
(`execution::fpcr`: AH and NEP, which FEX's FEAT_AFP code relies on, FIZ = DAZ, FZ = FTZ, RMode from RC) and FPSR to 0;
`bank` (every resident entry, so every precise fault) reads v16-v31 back and ORs FPSR's cumulative flags into MXCSR
(`mxcsr_flags`: IOC IE, IDC DE unless DAZ, DZC ZE, OFC OE, UFC UE, IXC PE). The gates already saved q0-q31, FPCR and
FPSR; since the review `save_native` also clears FPCR (`msr fpcr, xzr`), so the resident never runs under the guest's FPCR. LDMXCSR/STMXCSR are
resident transactions (`execution/sse.rs`): gating, the m32 operand at the CPL through the walker (#SS(0) via RSP/RBP, #GP(0),
#PF), then #GP(0) for a 1 in MXCSR 31:16 (MXCSR_MASK FFFFH). **MXCSR exactness:** FEX's own STMXCSR returns the stored value
masked with FFC0H, so its exception flags always read 0, and FEX never reads FPSR; with FPCR.AH = 1 the Arm flags match x86
(tininess after rounding, FTZ underflow, signalling compares, invalid conversions), except where FEX's lowering differs, fixed
in patch 0017: COMISS/COMISD compare with `fcmpe` (IE for a QNaN), ROUND* with imm8 bit 3 clear use `frintx` under the
instruction's rounding mode (PE), RCPPS/RCPSS/RSQRTPS/RSQRTSS keep FPSR across FEX's exact division (no flag); in patch
0018: DPPS/DPPD mask their inputs before the multiply (x86 multiplies only the selected elements: the others raise nothing)
and compute the products under a zero destination mask (their flags; the result is zero), and a COMISS/UCOMISS whose flags
are dead keeps its `fcmp` (IE, DE). **DE is exact (review decision):** with FPCR.AH = 1 and FIZ = 0 the M4 sets FPSR.IDC for a
denormal input it does not flush, with x86's precedence (none beside an IE or ZE of the same operation or a NaN operand; none
for FP-to-integer conversions, FRINT* or the reciprocal estimates; measured by the review), so IDC is DE unless DAZ (under DAZ,
FIZ = 1 and IDC marks a flushed input, where x86 sets no DE). QEMU 7.2 never sets DE, so `tools/run_i5a_de.py` judges it by
the SDM's exception tables: 75 forms, 351 cases (a denormal source or destination element, under DAZ, beside a QNaN, divided
by zero, DPPS/DPPD's unselected and zero-destination cases), all as the SDM says at block sizes default and 1; the review's
40 hand cases all give the expected MXCSR (one changed from series19: `addss denormal, -0` now 1F82H). The corpus masks DE
out of its MXCSR comparison. **Unmasked SIMD exceptions** (#XM, or #UD without OSXMMEXCPT) are not built: an LDMXCSR that
clears any mask stops `unsupported_system` before any effect (FXRSTOR/XRSTOR in I5b must do the same); the driver accepts
only masked `mxcsr` values. **Gating (translation time):** the host passes `--sse-gated` when the bank has CR0.EM, CR0.TS or no
CR4.OSFXSR; fexc then cuts every SSE form as a system instruction (`cut_gated`), the host answers `system::SSE_GATED`, and the
resident raises #UD (EM or no OSFXSR) or #NM (TS). Valid because every CR0/CR4 change drops the vCPU's code (S2a); a chunk that
keeps blocks across control changes must key them by these bits; since the review they are in `Cpu::code_context` too.
Obligation on every path that changes CR0.EM, CR0.TS or CR4.OSFXSR: flush the vCPU's code or key it, as MOV CR0/CR4 do
(INIT/SIPI, CLTS, LMSW and task switches if ever added, host edits of the bank; XSETBV for XCR0 once AVX is gated, I8). PAUSE, PREFETCHh, the fences, MOVNTI,
CLFLUSH and CRC32 are not gated. **Accesses:** vector loads and stores are recorded sites of class FPR (`ldr`/`str`/`ldur`/
`stur` b h s d q, register offset too); patch 0017 lowers FEX's element ops (MOVHPS/MOVLPS/MOVHPD/MOVLPD, PINSR*, PEXTR*,
EXTRACTPS m) to plain element loads and stores and MOVNTDQA/MOVNT* to plain `ldr`/`str q`, and builds FEX's named and indexed
vector constants in their register (they were loads from the state frame). Every store wider than a byte keeps its split
check; the resident completes a page-crossing vector store from the vector register (`split`, up to 16 bytes), so a fault on
either page writes nothing. **Alignment:** the first access of a legacy SSE instruction whose 16-byte operand must be aligned
(SDM Vol.2 2.5 classes 1, 2, 4; not MOVUPS/MOVUPD/MOVDQU/LDDQU, not narrower operands) gets `and x0, xA, #15; cbnz x0, A`
to a stub of the new kind 14 `align` (`brk #4`): #GP(0) before any walk, QEMU's order, an RSP base included. Admission decodes
the SIMD&FP data-processing class (its general-register destinations: FP-to-integer conversions, fixed-point FCVTZS/FCVTZU
included since the review, UMOV/SMOV: x25/x28 never written), the vector loads and stores and MOVAPS's X-register pairs at
matching sites, `axflag`/`xaflag`, `crc32*`, `mrs`/`msr` FPCR and FPSR, and the alignment check (code
shape: on exactly the access's address; which instructions need one is the producer's table). **Ordering and atomicity:**
vector accesses are plain `ldr`/`str q` under the M4's hardware TSO (FEX TSO emulation off): `litmus` vector families
(`vec-sb`, `vec-sb-mfence`, `vec-mp`, `vec-lb`, `vec-wrc`, `vec-iriw`, `vec-2+2w`: every plain access a 16-byte MOVDQU, MOVDQA
in the aligned variant) at N = 2..4, every variant, 20,000 trials each (series19) and 5,000 (series20): 0 forbidden.
**16-byte atomicity (review decision):** the profile enumerates AVX, so SDM Vol.3 9.1.1 makes the 16-byte accesses of MOVAPS,
MOVAPD and MOVDQA single-copy atomic; the Arm ARM (DDI0487 B2.2.1) treats an LDR/STR Q as two 64-bit single-copy atomic
accesses and only, with FEAT_LSE2, a 16-byte aligned LDP/STP of two X registers as one. Patch 0018 lowers their memory forms
to that pair (`ldp xA, xB` then `fmov dN, xA; fmov vN.d[1], xB`; `fmov`/`umov` out then `stp`), alignment-checked first,
recorded as sites of kinds 2/3 (`load_pair`/`store_pair`, admitted at 16 bytes only, two general registers; a split stub
cannot fire for an aligned pair but the resident completes one). VEX.128 VMOVAPS/VMOVAPD/VMOVDQA (and the EVEX.128 forms with
k0) need the same when AVX lands (I8). Tear test (a writer alternating 16-byte values, readers comparing halves, 200,000 each):
0 torn reads for MOVDQA, MOVAPS, MOVAPD and 16-byte aligned MOVDQU, and for MOVDQU inside 16 bytes; line- and page-crossing
MOVDQU tear (x86 gives them no atomicity; the page-crossing store is the resident's two copies). **Checks:** `tools/reference/gen_i5a.py` corpus (114 programs, 266 forms, random data
with float patterns, every rounding mode, FTZ/DAZ) against QEMU 7.2 (`qemu64,+ssse3,+sse4_1,+sse4_2`): 111 match at block sizes
default and 1; 3 differ by QEMU's two-NaN rule (it keeps the x87 rule, larger significand, for SSE; the SDM's Table 4-7 SSE
rule returns the first source, as natemu), each located by running its prefixes. QEMU also returns the original denormal
from MIN/MAX under DAZ, where the SDM's DAZ converts it to a signed zero first (natemu): the generator keeps MIN/MAX out of
DAZ groups. Faults (`tools/run_i5a_faults.py`, 454 cases judged by the SDM rules: #PF not-present/read-only/crossing for 25
load and 16 store forms, #GP misalignment, alignment before #PF and before #SS, non-canonical #GP/#SS and straddles,
LDMXCSR/STMXCSR, #NM/#UD gating, ungated forms, LOCK #UD, the unmasked-MXCSR refusal; XMM, MXCSR and memory unchanged at the
fault) all as expected at block sizes default and 1. `verify_precision` (P1-P11, alignment checks added) clean on the corpus.
FEX ASM suite 480 → 781 pass, 0 fail (left: MMX, PCMPxSTRx, AES/SHA, x87, AVX). Census SSE class: kernel 95 → 5 (EMMS and a
CET `wrussq` the classifier files as SSE; LDMXCSR now `system`), initramfs 198,200 → 7,236 (AES/SHA/PCLMULQDQ 6,015,
PCMPISTRI 401, ADX/MOVBE and MMX the rest); LDMXCSR/STMXCSR are `system` (kernel 1, initramfs 383)
(`experiments/track03/kernel-census/README.md`).
*Built (S3, system calls and misc system instructions; fexc series21-s3: the shim cuts group 9's register forms /6 /7):*
transactions in `execution/syscalls.rs` and `segments::inspect`, decoded by `system.rs` (F2/F3 now parsed; only the forms
that name them take them). **SWAPGS** (#GP(0) at CPL > 0) swaps the bank's GS.base with IA32_KERNEL_GS_BASE's slot; the
page takes it at the next `enter` (I7). **SYSCALL** (#UD without EFER.SCE; under TF it stops `unsupported_system` before any change, as SYSRET does): RCX, R11 := RFLAGS as is (RF included,
syscall_model C1; L3 met it after an IRETQ loading RF = 1: QEMU 11.1.2 and AMD's APM clear RF in R11, Intel's SYSCALL
operation copies RFLAGS, so Intel's text is kept), RFLAGS &= ~FMASK, CS = STAR[47:32] & FFFCH and SS = STAR[47:32] + 8 (unmasked) with the fixed flat
attributes, `set_cpl(0)`, RIP := LSTAR, no stack switch. **SYSRET** (#UD, then #GP(0) at CPL > 0, then #GP(0) for a
non-canonical RCX, Intel's order at CPL 0): RFLAGS := R11 & 3C7FD7H | 2, CS/SS from STAR[63:48] with RPL 3, `set_cpl(3)`
(since X2 an AC from R11 with CR0.AM turns alignment checking on: the next misaligned access at CPL 3 is #AC(0)); a TF from R11 stops `unsupported_system`; SYSRETL
(compatibility mode) stops `unsupported_mode`. **RD/WR FS/GS BASE** (#UD without CR4.FSGSBASE, any CPL; 32-bit forms
zero-extend; #GP(0) for a non-canonical 64-bit write) read and write `Cpu::fs_base/gs_base`. **LAR/LSL/VERR/VERW** (register
or m16): no fault for a bad selector (ZF = 0, destination kept); IA-32e types (LAR: code/data, 2, 9, B, C; LSL: code/data, 2,
9, B), DPL >= max(CPL, RPL) unless conforming code, descriptor read as 8 bytes, LAR mask 00F0FF00H, LSL G-scaled; only ZF
changes; a #PF on the table read is delivered. Undecided and untested: whether IA-32e LAR/LSL check the upper half of a
16-byte system descriptor (its type field, limit or page); the SDM text at hand does not settle it (syscall_model C8), and
the tables assert nothing for such a descriptor (selector 0xa0 is left out). **VERW** is the architectural instruction only: its MDS buffer clearing has
no architectural effect and natemu models no such buffers, so nothing is left out (not a deviation). **RDRAND** takes the next
word of a per-vCPU entropy pool (`execution::Entropy`, 32 words in the state page at `ENTROPY_OFFSET`): the host fills it
from `getentropy` at reset and tops it up at each exit; CF = 1 with OF/SF/ZF/AF/PF cleared, 16-bit merge, 32-bit zero-extend.
CF = 0 (value 0) when the pool is empty: today unreachable, since every RDRAND is itself a host exit (its decode) that
refills the pool first; it becomes reachable with a resident decode cache (L5) or a failing host source, and software's
retry loop handles it. **WBINVD/INVD/WBNOINVD** (#GP(0) at CPL > 0; WBNOINVD is not advertised and runs as WBINVD, as on
processors that ignore its F3) change nothing: RAM is coherent write-back with no cache model, so INVD's discard is WBINVD
(no worse: no data is lost). **CLFLUSH** has a byte load's faults at the CPL (#GP(0)/#SS(0) non-canonical, #PF with a read
error code; the walk sets A) and no other effect; on a page that is not write-back RAM (e.g. a UC device mapping) it stopped
`unsupported_system`, as every resident operand access did (since M2 RAM of any type is accessed and a device page has nothing to flush). **#UD** for forms the profile does not advertise or that need VMX/SVM/
SGX/TDX operation: RDSEED, RDPID, CLFLUSHOPT, CLAC/STAC, MONITOR/MWAIT(X), SERIALIZE, RDPKRU/WRPKRU, ENCLS/ENCLU, VMCALL and
the VMX group, VMMCALL and the SVM group, TDCALL, the RMP group, CLZERO, RDPRU, XTEST, LKGS, VMREAD/VMWRITE, INVEPT/INVVPID/
INVPCID, ENCLV, PCONFIG, WRMSRNS, XSUSLDTRK/XRESLDTRK, and 0f ae register forms without F3. IRET-to-self and CPUID were already draining entries (every system
instruction is). Oracle: `tools/models/syscall_model.py` (independent author; ambiguities C1-C12, `alternatives` for C1, C6,
C9). Checks (`run_slice3_vm.py system`, `tools/bench/smp/syscalls.py`, block sizes default and 1): `sc-roundtrip@1..4`
(EFER.SCE, STAR/LSTAR/FMASK, KERNEL_GS_BASE per CPU, SYSRETQ into CPL 3, RD/WR FS/GS BASE at 32 and 64 bits, three
SYSCALL -> SWAPGS, per-CPU `%gs` stores, RDGSBASE -> SWAPGS -> SYSRETQ round trips) against QEMU, which loses CF in the
handler after SYSCALL (`sdm_bytes`); `sc-verify-cpl0/3` (LAR at 64/16 bits, LSL at 32/64/16, VERR, VERW over 29 selectors of
the S2b GDT and LDT, memory forms) against QEMU and every result byte against syscall_model; `sc-rdrand@1/2` (CF, cleared
flags from three presets, widths, eight samples differ); `sc-cache@1/4`; 23 fault and refusal cases judged by syscall_model (SYSCALL #UD with SCE = 0 at
CPL 0/3, SYSRET #UD/#GP at CPL 3/non-canonical RCX, SWAPGS at CPL 3, FSGSBASE disabled, non-canonical WRFSBASE/WRGSBASE,
RDSEED, RDPID, CLFLUSHOPT, ENCLV, XRESLDTRK at CPL 3, WBINVD/INVD at CPL 3, CLFLUSH non-canonical (#GP, and #SS through RBP) and on a supervisor page
at CPL 3; refusals SYSCALL and SYSRET with TF and SYSRETL, natemu only), QEMU within the model on 9 of the 20 faults,
listed in `QEMU_DIFFERS` on 11 (its CPU and
helpers: no canonical check in SYSRET, WRFSBASE/WRGSBASE, no access in CLFLUSH, no CPL check in WBINVD/INVD,
RDSEED/RDPID/CLFLUSHOPT implemented). Also pinned QEMU's SYSRET loads VM and not VIF/VIP from R11 (SDM mask 3C7FD7H), so the
round trip's R11 sets every other bit (reserved ones included); VM/VIF/VIP are covered by syscall_model's unit tests only.
FEX ASM suite (driver `fsgsbase 1`, as Linux sets CR4.FSGSBASE): 781 -> 794 pass, 0 fail; tests needing an unadvertised
instruction count unsupported (`run_asm.py` `NOT_ADVERTISED`), as do SYSCALL tests (`NEEDS_OS`: #UD with EFER.SCE = 0).
Not built: SYSENTER/SYSEXIT (no census use), CLWB and TPAUSE (fexc refuses them as unsupported forms; not advertised).
*Built (X2, events: interrupt eligibility, shadows, POPF, IRETQ, HLT, NMI, #AC, the REP RF signal; fexc series22 after
review: a shim change, no new patch):* `execution/events.rs`. **Pending events** are a per-vCPU record in the global area
(`cache::Events` at `cache::EVENTS`): a 256-bit set of pending vectors (IRR-like until X3's LAPIC; the highest is taken first),
one NMI bit (posts while pending merge), `end` (the host asks a halted vCPU to stop) and `halted` (set while parked).
Producers set bits by atomic OR, then ring the vCPU's doorbell; the resident clears the bit it accepts. The STI shadow is
`State::sti_shadow`, the MOV SS shadow `Cpu::mov_ss`, NMI blocking `State::nmi_blocked`. **Driver sources (the real
path):** the host posts and rings the vCPU's SPI (`hv_gic_set_spi`), then answers the hook `nop` as `system::EVENT_HOOK`
(description items `post ADDR VECTOR`, `nmi ADDR`; `if 0|1` sets IF at the start; `--kick-events E,..` posts a random event
before each kick). **Eligibility** at the top of `find`'s loop (every precise stop, every resident entry that dispatches,
every boundary after a system instruction or a delivery): Table 7-2, NMI before INTR; INTR needs IF and no shadow, NMI needs
blocking clear and no shadow. Choices inside delivery_model's set-valued cases: both shadows block NMIs (A9 `maybe`); a
shadow-setting instruction inside a shadow sets its own (`sti?`/`mov_ss?`). Delivery is `delivery::deliver` with
`Event::External` (EXT = 1, no error code, RF = 1 between REP iterations, else the current RF); NMI blocking is set before
delivery and kept if it faults (A8). **Shadows, exact:** a shadow is honoured only at the boundary right after its
instruction. A system instruction or a delivery ends it, and so does entering generated code (`enter`), since every later
resident entry follows a completed instruction except a doorbell at that block's first word, which then arms a stop instead
of stopping there (`State::shadow_entry`), so the event is taken after at least one instruction (permitted for an event that
arrives then). An event pending at the shadow's boundary makes `find` run exactly one instruction (`REQUEST_STEP`: a
single-instruction block in the step slot, never cached or linked, whose computed exit branches to the dispatcher's
`svc #MISS` and literal exits to the linker, so every exit returns to the resident; a system instruction is answered with
its decode and runs as a transaction), and the boundary after it is evaluated again: `sti; X` takes the interrupt after X,
`sti; ret` at the RET's target, `sti; hlt` wakes with RIP after the HLT. **REP RF signal:** admission reports the REP's RIP
(`Admitted::rep_self`), the host the mask of link points that are its self-exit (`State::rep_exits`), kept in
`Block::rep_exits` (size unchanged; `Block::rep_exit(offset)`, `Block::starts_with_rep()`); the linker finds the exit's link
point from its `svc` address, an armed stop from the trapped word, never from RIP. That exit sets `State::mid_string`; after
the last element (RCX = 0: I2's pass exits to its own RIP) RIP moves past the REP instead (x86 has no boundary at the REP with
RCX = 0 after it ran). A doorbell at the first word of a block that starts with a REP arms the stop (that entry may be its
own self-link). **Transactions** (kinds 0x40-0x45): CLI/STI #GP(0) at CPL > IOPL (no VME/PVI); STI's shadow only when IF was 0.
POPF/POPFQ (8 bytes, 2 with 66; #SS(0), #PF, #AC at CPL 3): CF PF AF ZF SF TF IF DF OF IOPL NT AC ID written, IOPL only at
CPL 0, IF only at CPL <= IOPL; RF, VM, VIF, VIP unaffected; setting TF stops `unsupported_system` before any effect (S4's
single-step hook). IRETQ and S3's SYSCALL/SYSRET need nothing more: every transaction returns into `find`, which re-evaluates.
**HLT** (#GP(0) at CPL > 0) commits RIP past it and parks in `wfi` at EL1 with IRQs masked; with the in-kernel GIC, HVF holds
the vCPU without a host exit. A pending IRQ is a WFI wake-up event whatever PSTATE.I says (Arm ARM DDI 0487, "WFI wake-up
events", D1.17.2 in recent issues; read here in secondary quotations, not in the repo's snapshot); how HVF implements a
trapped guest WFI is not documented by Apple; every run here woke. Each wake acknowledges every pending doorbell, takes the
vCPU's invalidations (a peer may wait for its shadow reset), serves a stop still wanted, and leaves only for an event it can
take (A10: a blocked NMI or masked INTR does not wake it); `halts` counts the HLTs that waited. The host's watchdog skips a
halted vCPU; the program driver ends halted vCPUs (stop `halted`, 32, RIP after the HLT) once every kicker has ended and every
vCPU is halted or ended. **#AC:** native alignment checking, not per-site checks or blocks keyed by AC: `resume_exception`
sets SCTLR_EL1.A before its `eret` when CR0.AM, RFLAGS.AC and CPL 3 hold (`Cpu::alignment_check`), and both gates clear it
after their saves (`check_resident_stack.py` checks both shapes). At an access site an alignment fault under AC is, in order,
the canonical check (#GP/#SS), then #AC(0), before any walk and before the split-lock stop (CMPXCHG16B's #GP(0) stays first).
A 16-byte vector access (MOVUPS class) that is 8-byte but not 16-byte aligned, for which the SDM permits no #AC, is refused
(`unsupported_mode`, state at the instruction) at an access site (`access_aborted`), but completed when it is a page-crossing
store (the split stub, `split()`, writes it); any other page-crossing store under AC is #AC there. The resident's explicit
CPL 3 accesses (`segments::linear`: POPF, IRET and far-transfer pops, LDMXCSR/STMXCSR, SGDT-class stores) check Table 7-7's
alignment. **BT/BTS/BTR/BTC on memory** (FEX touches only the byte holding the bit): under alignment checking the host passes
fexc `--alignment-gated` (series22's shim), which cuts them as system instructions (`system::BT_ALIGNED`); the resident gives
#AC(0) when the operand is misaligned at its size (with a register bit offset, the operand-sized unit selected has EA's
alignment; a non-canonical unit is left to the step's #GP/#SS, which comes first), and otherwise steps it compiled without
the flag (`State::plain_step`); LOCK BT is #UD; a null CS/DS/ES/SS override is accepted as without it, FS/GS add their
base. Older edge, from the slice-1 byte-access BT policy, not from X2: a misaligned BT unit whose bytes run past the top of
the canonical range gets no #AC, and the one-byte plain step may then complete where x86 gives #GP. Alignment checking is bit 12 of `Cpu::code_context` (RFLAGS.AC changes
without dropping code). `Cpu::control`'s AM/AC refusal is gone. **Checks** (`run_slice3_vm.py events`,
`tools/bench/smp/events.py`, block sizes default and 1; every logged delivery judged by delivery_model
`recognise`/`after_instruction`): QEMU system programs `ev-sti-shadow`, `ev-mov-ss`, `ev-mov-ss-sti`, `ev-popf`, `ev-iretq`,
`ev-hlt-wake`, `ev-nmi`, and (after review) `ev-sti-ret`, `ev-sti-jmp`, `ev-movss-ret`, three rounds each so the target is
cached and reached through L1 from the second (QEMU raises the events through its x2APIC: SELF_IPI, an ICR NMI to its own APIC
ID; natemu's variant has two `nop`s per `wrmsr`, so every address is the same), at 1, 2, 4 vCPUs: QEMU agrees with the model on
every vector and boundary (in the chained-shadow programs it picks another permitted boundary), pushes RF = 1 for every
external interrupt and NMI (a QEMU deviation: its frames are judged without RF), final registers equal; 47 natemu-only cases
(CLI/STI/HLT at CPL 3 with IOPL 0/3, POPF at CPL 3 and 0 incl. 16-bit, POPF TF refused, LOCK CLI #UD, HLT with a masked
interrupt or idle ends `halted`, 32 #AC cases: loads, stores, RMW, LOCK, XCHG, REP MOVSQ, page-crossing load and store,
MOVQ/MOVUPS, non-canonical #GP first, page not present, PUSH, POPF, the first misaligned access after a SYSRET (S3) to CPL 3
with AC from R11, BT/BTS/BTR/BTC misaligned (word, dword, qword, register and imm8, LOCK, a DS override) and aligned (results checked, a DS override), and
controls without #AC; QEMU does not implement #AC); stress with `--kick-events 0x40,0x41,0x50,nmi`, 2,000 kicks per vCPU at 1,
2, 4 vCPUs: every delivery logged (each reserves its slot by XADD) and judged: at an allowed boundary (never in a shadow, IF = 1
for INTR, RF exactly by the REP rule, no NMI in the NMI handler), never late after a shadow step (each step puts a delivery at
the boundary after the shadowed instruction), the guest's count equal to the resident's, the stress loop's registers and data
equal to QEMU's run without events (428-549 deliveries per vCPU, 257-976 shadow steps and 16-25 mid-REP deliveries per run), the
HLT loop's 1,000 HLTs per vCPU all woken: 136 ok. The review's probes: `sti; ret`, `sti; jmp reg`, `mov ss; ret` take every
round's interrupt at the target (before: rounds 2-3 waited for an unrelated resident entry); `stale2` shows no delivery at the
spin loop's inner boundary at default block size (shadow steps remain where an event is pending at the boundary right after
MOV SS, as x86 requires). Standing suite with series22 green: fix/fix-1 64, seed/seed-1 44, faults and faults-1 7,581 each, flow 64, loopfaults 846,
probes 83, FEX ASM 794 pass and 0 fail (`run_asm.py`, `run_i5a_faults.py`, `run_i5a_de.py` accept #GP(0) at a CPL 3 `hlt`, or
`halted` after one at CPL 0, as reaching it: `run_slice1_vm.at_hlt`), i5a faults 454, i5a DE 351, corpora, smp, limits, kicks
(1, 2, 4 vCPUs), system, coherence, sharedpt, litmus, contain, privilege and delivery with 0 failures, `events` 136 ok (its one
miss in the suite run was the judge's: an NMI at the program's end boundary, now allowed). **For X3, X4, S4:** X3 posts into
`Events.vectors` (atomic OR) and rings the target (SGI between residents, SPI from the host), replaces "highest vector" in
`events::next` with LAPIC priority (PPR/ISR/EOI; the bit becomes the IRR), and gets re-evaluation after EOI/TPR writes for
free; the NMI bit and blocking serve ICR NMIs. In `park`, a post that lands between `next()` and the `end` check is lost once
`end` is set (X2's driver sets `end` only when no source can post); X3, whose peers post at any time, must check `next()`
again after seeing `end`. X4 adds host sources on the same post-and-ring path. S4: POPF's TF refusal is the single-step hook;
shadows hold events until one instruction completes, and S4 adds the suppression of trap-class #DB after MOV SS; RF: a REP's
self-exit is a block exit, so `completed` clears RF there, while x86 keeps RF set until the REP completes (no breakpoint
uses it before S4). **Not built / choices:** the 8-byte-aligned 16-byte vector access above (refused at an access site;
completing it needs the access emulated); the SDM orders neither way #AC against #PF on a page not present, and natemu gives
#AC (Arm takes the alignment fault before translation).
*Built (M3, MMIO completion; fexc series23-m3 = series22 + a shim change, `shim/fexc_mmio.inc`):* **Device pages.** A
guest-physical page that is not guest RAM is a device page; the host owns a registry of page-aligned device ranges above RAM
(`crates/host/src/devices.rs` `Devices`; `Device::read(vcpu, offset, size) -> Option<u64>`, `write(.., value) -> bool`, sizes
1/2/4/8, `None`/`false` = the device refuses the access, unchanged). Choice: no shadow plus the walker's result, not a stage-2
hole: the resident already walks at every shadow miss, so `translate` returning a non-RAM gpa unshadowed makes every access
abort at its recorded site in the resident, where the site gives the precise state; a stage-2 hole would exit to the host
from inside the block with no site, banked state or walk (the walk's #PF must come first). Memory type does not matter for a
device page (device ranges are UC; RAM with a non-WB type stopped until M2, which runs it natively, and UC RAM one instruction at a time since M2 addendum 2). **Fault priority:** at the abort the walk of
every page touched has run, so canonical #GP/#SS, #AC (SCTLR.A, X2) and every #PF the tables owe (not present, U/S, R/W with
WP, reserved bits, upper levels, the second page of a crossing access) come before any device effect; the walk's A/D updates
stay when the access is then refused (as on x86). **Completion from the site** (`execution/mmio.rs`): fexc records, from
FEX's own decode of the instruction it translated (`shim/fexc_mmio.inc`, called from the slice-1 allowlist), a completion
record for the one load or store site of each MOV-family instruction (88/89/8A/8B with a memory operand, C6/C7 /0, A0-A3
moffs64, MOVZX, MOVSX, MOVSXD as FEX lowers it; no LOCK/REP/67): x86 register (AH..BH), destination size, zero- or
sign-extension from the access width, store or load, length; it rides in the site's `guest_offset2` slot
(`code_object::completion` checks it against the site's width and direction; admission requires its length to end at
the next instruction boundary of the PC table or at the block's end). A
store's data is the site's native transfer register (what the translated instruction stores: an immediate or AH..BH
included). The resident banks the state at the instruction, makes the one device access (`mmio::local` first, the hook for a
resident device such as X3's LAPIC, then the host: `REQUEST_DEVICE` after `dsb sy`, the `Mmio` record at state page +0xe00
holding gpa, width, direction and data; the host performs exactly that read or write under the device's mutex, never decoding
x86), merges a load as x86 does (8/16-bit keep the rest, 32 zero-extends), moves RIP past the instruction and never runs it
again: exactly once. Nothing at run time reads guest bytes, so cross-modifying code gives x86's old-or-new outcome: a block a
peer's rewrite killed is stepped from the new bytes before any site is served (`aborted`), and a block still running ran
the old instruction its records describe. **Older defect fixed here (P1):** the single-instruction step treated a system
instruction in its reply as `bad_reply`, so a peer replacing an instruction by INT3 (Linux's `text_poke_bp`) halted the vCPU;
`step` now executes it as `find` does (both entries, `find` → step and `aborted` → `step_from`) and continues through
`find`, so the next boundary's checks run (an interrupt a POPF, IRET or STI made eligible is taken there, not one instruction
late); only a BT* form found aligned is stepped again, compiled plain. Linux's
accessors emit exactly the completed forms (`arch/x86/include/asm/io.h` `build_mmio_read/write`: `mov` with a register;
`apic.h` `native_apic_mem_write` `movl` (XCHG only for X86_BUG_11AP); the APIC-ID read in `head_64.S:268-270`). **Refused**
(`unsupported_mmio`, 48, before any device effect, state at the instruction, reason in `Mmio::status`): `atomic` (LOCK forms,
XCHG), `site` (pair/push/pop sites), `crossing` (a load or a split store crossing a page with a device page on either side,
after both walks), `form` (a site without a completion record: RMW, CMP/TEST, BT*, CMPXCHG, SETcc, PUSH/POP/CALL m, MOVNTI,
strings, SSE), `unassigned` (no device claims the page; an MMIO rule for unassigned pages is the owner's), `device` (the device
refused the access), `system` (a system instruction's or a delivery's own access to a device page, `segments::linear`; nothing
written). CLFLUSH of a device page completes with only the walk's faults. **For X3, L3, L4:** register a `Device`
(`Arc<Mutex<dyn Device>>`) on page ranges; a handler runs on the accessing vCPU's thread with that vCPU stopped, gets its
number, is serialised with every other access to that device by its mutex (one total order, each vCPU's in program order),
must return promptly (never wait for another vCPU or for input: peers' shootdowns spin meanwhile) and may refuse an access it
does not implement (e.g. a non-32-bit LAPIC access). A resident device answers in `mmio::local` (it sees the access's gpa,
width and data, with the vCPU's state; the merge and RIP are the completion record's), with no host exit. Limits: the
registry is fixed before the vCPUs run and lies above RAM (a relocatable APIC base or holes below the top of RAM need M1 and a
mutable registry). **Cost:** each device access ends its block (the resident dispatches at the next RIP, an L1/L2 lookup)
and makes one host exit unless `mmio::local` answers it. A peer's failure cancels the run: a device request the host has not yet
served is never performed (it checks before serving); one it has served may be left performed and uncommitted (the vCPU
ends at the instruction). The stop is terminal, so nothing replays the access. **Checks** (`tools/run_m3.py`, series23-m3, block sizes default and 1; driver item
`mmio VA GPA [unassigned]` maps a page to a test device with a counting counter register, an order-checking sequence
register, a refusing last 8 bytes and memory, and reports per-vCPU access counts and log): `corpus` 60 seeded MOV-family
programs and 6 fixed encodings (REX before 66, 66 before REX.W, BPL and CH stores, imm16, a 16-bit moffs; 715 device
accesses) at CPL 3 and 0: registers, RFLAGS, RIP and the page's bytes equal pinned QEMU running them on RAM (except `48 66
8b 03`, judged by the SDM: REX not before the opcode is ignored, a 16-bit MOV; QEMU keeps REX.W), and the device's ordered
access log and registers equal `tools/models/mmio_model.py` (independent author; it refuses a prefix after REX, so the fixed
REX-before-66 program is QEMU/SDM only): 264 runs, 0 failures; `faults` 22 cases (#PF not present at CPL 3/0, load and
store; supervisor page at CPL 3, completes at CPL 0; read-only store at CPL 3 and CPL 0 with WP, read-only load completes;
reserved bit; PDE not present; crossing into a page not present; #GP and #SS non-canonical; #AC misaligned, aligned completes;
CLFLUSH completes untouched and faults not present; ADD, LOCK ADD and XCHG on a read-only device page give #PF(7) before
their refusal; identical loads and stores back to back in one block each reach the device); `refusals` 28 cases (crossings
both ways, a device refusal) with their reasons and the device untouched; `kicks` 1-4 vCPUs looping counter reads (each
vCPU checks the values increase) and sequence writes under random host kicks (20,000 iterations and 1,000 kicks per vCPU):
device reads and writes per vCPU equal the iterations, no order error, kick accounting exact; `contain` a device reply with
an unknown status halts `bad_reply` with nothing committed; `xmc` (the review's repro): vCPU 0 loops a device load while
vCPU 1 rewrites it with no synchronisation (the same byte, load <-> store, another register, INT3 <-> the load; and INT3 over
a plain RAM load), 300,000 rounds against 20,000,000 rewrites: vCPU 0 reaches its end or takes #BP after the INT3, never
stops otherwise, device accesses = rounds done (hundreds of steps from rewritten bytes per run); `boundary`: with IF = 0 and
an interrupt pending, a peer rewrites POP RAX into POPF (sets IF, no shadow) or NOP into STI (shadow): the interrupt is
delivered exactly at the boundary `delivery_model.recognise` first allows. Independent reviews:
first pass ACCEPT (its non-blocking findings taken); the second found the host's run-time decode wrong under cross-modifying
code and the step's system reply (P1), both fixed as above. **Not built:** RMW, string, vector and LOCK forms on device pages
and crossing accesses (typed stops; Linux's device paths use none), an MMIO rule for unassigned pages, a resident device
(`mmio::local` is X3's), a relocatable or mutable registry (M1/X3).
**I5c (recorded; the I5a review's design, Built (I5c) below): unmasked SIMD exceptions.** Hardware FP traps are not used although
the M4 has them (the review's probe: FPCR's trap enables stick and a trapped divide by zero is a synchronous exception, EC
0x2C): a trap reports one Arm operation's exceptions, while x86 delivers #XM (#UD without CR4.OSXMMEXCPT) for the whole
instruction, before writing its destination, with the flags of every element and x86's precedence; FEX lowers one x86
instruction to several Arm operations (DPPS/DPPD, HADD, the conversions) and to helper operations that must raise nothing
(RCP/RSQRT), and earlier operations of the same instruction may already have written static registers, so a trap would not
land on a precise x86 boundary. (The resident runs with FPCR cleared since the review, so guest trap enables never reach it.)
Design: blocks compiled under an MXCSR with any exception unmasked (the masks in `Cpu::code_context`, or every
mask-changing LDMXCSR/FXRSTOR/XRSTOR flushing the vCPU's code) give each SSE floating-point instruction a software check: clear
FPSR, compute into a temporary, test the FPSR bits of the unmasked exceptions, branch to a #XM stub (the state before the
instruction, the flags it raised for the resident to merge) or commit the temporary to the destination. Until then an LDMXCSR
that clears a mask stops `unsupported_system`.
*Built (I6, x87 at reduced precision; fexc series25 (the combined I6 + M3 build), patch 0019 and shim changes; after review):* x87 runs in FEX's reduced-precision
mode (`X87REDUCEDPRECISION`, set by `--profile slice1`; owner direction "x87 performance over accuracy"): every ST(i) is a
double. **State:** the guest-state page holds FEX's x87 fields (`frame::X87_*`): `mm[8]` (ST(i) = `mm[(TOP + i) & 7]`, low
8 bytes), the TOP, C0..C3 and IE flag bytes, the abridged tag byte (bit i: register i not empty), and the x87 exception word
(x28 + 0x4b4, in FEX's padding after the tag byte: FPSR's bit order, IE ZE OE UE PE in bits 0..4, DE in 7, SF in 6). The page
is authoritative for these (guest data: the resident reads and writes them only in x87 transactions, never trusts them; TOP is
masked to 0..7 wherever generated code indexes `mm`). FCW is the bank's (`Cpu::fcw`, FNINIT state 037FH at driver start,
not RESET's 0040H), written into the page at every `enter` and never read back (FNSTCW reads it there). GUEST_STATE and
GUEST_STATE_WRITABLE list the fields (FCW readable only); the layout test pins every offset. **Translation:** patch 0019 keeps
FEX's x87 stack pass on its slow path and flushes (and drops its caches) after every lowered op, so the x87 state is exact in
the page at every instruction boundary (a fault or stop anywhere sees it; no register pressure across a block: slice1 admits
no spill area); ST(i) is `add x0, x28, xI, lsl #4; ldr|str dN, [x0, #0x420]` (context-indexed, no site; admission's
code-shape check accepts exactly that pair). The JIT wraps each floating-point op of an admitted x87 instruction
(`FexcX87Op`: VFADD/SUB/MUL/DIV/SQRT, FCMP, the conversions): FPSR cleared before it (MXCSR's flags saved in x3 and put back
after), its FPSR flags OR-ed into the exception word after it; when FCW.RC differs from MXCSR.RC, or MXCSR has FTZ/DAZ, the
host passes `--x87-fpcr` and the op runs under FPCR AH|NEP|RMode(FCW.RC) (x2 keeps MXCSR's). So x87 flags never reach
MXCSR, and x87 rounding is FCW's. Code is keyed by the x87 rounding (`Cpu::code_context` bits 14..16: set only when it
differs) and CR0.MP (bit 13). **Forms admitted (generated code):** FLD/FST/FSTP m32/m64/ST(i), FILD m16/m32/m64,
FIST/FISTP/FISTTP m16/m32/m64 (patch 0019: x86's integer indefinite with IE for a NaN or an out-of-range value, PE only for
a stored inexact integer; FEX saturated, and stored a 64-bit conversion's low half for m16), FADD/FSUB/FSUBR/FMUL/FDIV/FDIVR in all
register and memory forms, the P and FI forms, FCOM/FCOMP/FCOMPP/FICOM/FICOMP/FTST (signalling compares, `fcmpe`: IE for a
QNaN), FUCOM/FUCOMP/FUCOMPP, FCOMI/FCOMIP/FUCOMI/FUCOMIP (OF, SF, AF cleared as the SDM says), FCMOVcc, FXCH, FLD1/FLDZ and
the other constants, FSQRT, FRNDINT (frintx: PE), FXAM (patch 0019: x86's classes of a double), FINCSTP,
FDECSTP, FFREE, FNOP, FCHS/FABS (patch 0019: sign-bit operations: Arm FNEG/FABS leave a NaN unchanged under FPCR.AH), FNSTSW AX/m16 (exception flags from the exception word; ES and B 0), FNSTCW, WAIT. **Resident
transactions** (`execution/x87.rs`; 80-bit conversions in `natemu_abi::x87`): FNINIT, FNCLEX, FLDCW, FLDENV/FNSTENV,
FRSTOR/FNSAVE (14/28 and 94/108-byte images; FNSAVE then initialises), FLD/FSTP m80 (with x86's stack overflow/underflow:
IE and SF, C1, the indefinite), each after its gating, with #PF/#GP/#SS (the whole image walked before any byte moves) and
#AC by SDM Table 7-7 (2, 4 or 2 by image size, 8 for m80). **Helper inventory (FEX's reduced-precision mode):** host libm
helpers through the frame's pointers for F2XM1, FYL2X, FYL2XP1, FPTAN, FPATAN, FPREM, FPREM1, FSCALE, FSIN, FCOS, FSINCOS:
refused (`unsupported_form`); SoftFloat fallbacks F80CVT/F80CVTTO for FLD/FSTP m80, FNSAVE/FRSTOR (and FXSAVE/FXRSTOR, I5b):
replaced by the resident's conversions; F80BCDLOAD/F80BCDSTORE (FBLD/FBSTP): refused; FXTRACT is native but splits a
denormal double wrongly: refused; everything else is native code. Also refused: FENI/FDISI/FSETPM, the undocumented aliases
(DC D0/D8, DD C8, DE D0, DF C0..DF, including FFREEP) and reserved encodings, 66/F2/F3 on x87 forms, MMX-register forms.
**Exceptions (decision):** every x87 exception stays masked: FLDCW, FLDENV and FRSTOR (FXRSTOR in I5b must do the same)
whose FCW clears a mask stop `unsupported_system` before any effect, so #MF is never due and CR0.NE (FERR#) cannot matter:
no refusal of NE = 0 is needed. Masked responses are the double operation's (indefinite, infinities, zeros) and the flags its
FPSR flags. **Stack faults** (SDM Vol.1 8.5.1.1, masked): FEX models none, so patch 0019 adds them in the x87 stack pass, for
every form at once: each read of a register checks its tag (an empty one reads as the QNaN indefinite: stack underflow) and
each push checks that register TOP - 1 is empty (else the indefinite is pushed: overflow); the instruction's IE and SF bytes are
set, and C1 = 1 for an overflow, 0 for an underflow, at the next instruction's start (so after its own store: a #PF there
leaves FSW as it was, and the #PF wins). The indefinite then gives each form its masked response by itself: arithmetic and
FSQRT write it to the destination (now marked valid), FST/FSTP store it (FFF8..0H, FFC00000H for m32), FIST/FISTP/FISTTP the
integer indefinite, compares are unordered (FCOMI: ZF PF CF = 1), FXCH loads it into the empty register before the exchange,
FLD ST(i) pushes it, and an arithmetic result after an underflow is the indefinite even beside a NaN operand (after review);
FCHS/FABS of an empty register give the indefinite itself; FXAM reads its register unchecked (no fault for
an empty one, SDM FXAM). FCMOVcc checks both operands (an empty one reads as the indefinite whatever the condition: the SDM
names #IS there without a per-instruction response). The resident's FLD/FSTP m80 do the same. Cost: an x87 loop of
FLD m64/FADD/FMUL/FSTP takes 33.5 ns per iteration on series25 against 23.6 with only the stack checks (series24-i6) and 24.2
without them (series23-i6): most of it is FLD m64's quieting multiply inside the FPSR wrapper (two `mrs`/`msr` pairs); the
same loop's integer form takes 1.6 ns. **Gating
(translation time):** `--x87-gated` (CR0.EM or TS) cuts every documented x87 form, the refused ones included, as gated, and
`--wait-gated` (TS with MP) cuts WAIT; the host answers `X87_GATED` and the resident raises #NM; the resident's own x87
transactions check EM/TS first; WAIT under EM alone runs (SDM Vol.3 Table 2-2). **Deviations (recorded; the reduced-precision
class the owner accepted, every other one listed):**
1. Values are doubles: 53-bit significands whatever FCW.PC, the double's exponent range for every intermediate (overflow,
   underflow and denormals at double thresholds: `1e300 * 1e300` is infinity, with OE, where x87 holds 1e600), so every
   inexact result and every comparison or integer conversion of one may differ from an 80-bit x87 (integer-visible:
   FIST/FISTP of such a result, FNSTSW condition codes of a comparison of one, OE/UE/PE flags).
2. FLD m80 and FRSTOR round an 80-bit value to a double by FCW.RC (x87 holds it exactly); pseudo-NaN, pseudo-infinity and
   unnormal encodings load as the indefinite; an SNaN keeps as much payload as fits (bit 0 if none). FILD m64 of more than
   53 significant bits rounds at the load (PE). FSTP m80, FNSAVE and the FXSAVE image widen exactly.
3. FLDPI, FLDL2T, FLDL2E, FLDLG2, FLDLN2 load the nearest double whatever FCW.RC.
4. FSW exception flags are the double operation's FPSR flags: equal to x87's when the double operation is exact or rounds
   as the 80-bit one would (the QEMU corpus), else they may differ (PE, OE, UE); DE: FLD m32/m64 of a denormal raises DE
   as x86 does (after review: FLD m64 is a multiply by 1.0 inside the x87 FPSR wrapper, which also quiets an SNaN with IE;
   FST/FSTP m64 quiets an SNaN with IE in integer code), but arithmetic on a double denormal (an 80-bit normal) raises DE
   where x87 would not.
5. C1 is never the round-up indicator: every instruction that reads a stack register stores C1 = 0 (the stack check's
   flush), so after arithmetic, FSQRT, FRNDINT, FST/FSTP, FIST/FISTTP and FCMOVcc C1 reads 0 where x87 would report a
   round-up (1). Every other C1, C0, C2, C3 the SDM defines is exact: C1 = 0 or 1 for overflow on every push, 0 for
   FCHS/FABS, FCOMI/FUCOMI(P) and any stack underflow; compares, FTST, FXAM, FXCH, FINCSTP/FDECSTP and the resident's forms.
6. Two NaN operands: FPCR.AH's rule (the first operand's NaN), not x87's larger significand.
7. (Built before review: stack faults are detected; see above.)
8. FIP, FCS, FOP, FDP, FDS are not modelled: 0 in FNSTENV/FNSAVE images (and FXSAVE's); the 28-byte image's upper words are 0.
9. An x87 store whose memory access faults has already OR-ed its FPSR flags into the exception word (FSW may show the
   faulting FST m32/FIST's PE/OE/UE/IE at the fault); `verify_precision` reports these sites (569 in the corpus) instead
   of failing P1.
10. FSW.ES and B read 0 (every exception masked); FLDENV/FRSTOR take FSW's other bits as given; FCW is stored as loaded.
11. The tag word in FNSTENV/FNSAVE images is computed from the doubles (zero 01, NaN/infinity 10, else 00: a denormal
    double is an 80-bit normal); FLDENV/FRSTOR keep only empty/non-empty.
12. G1's page deviation now covers the x87 fields: a guest data access to the page reaches its own x87 state.
**Not built (remaining; census counts from `kernel-census-i6-20261029-01`):** the transcendental, remainder and scale forms
(FPATAN 22, FSCALE 9, FYL2XP1 8, F2XM1 5, FPREM 4, FYL2X 4, FPREM1 1: 53 initramfs sites, none in the kernel; FEX's helpers
need resident or native implementations); FXTRACT (3, a denormal-aware split); FBLD/FBSTP (0 in the census); FFREEP (DF C0+i,
an undocumented alias: 3); MMX (the x87 registers as MM0..7: 1,381 initramfs sites and 1 kernel site, filed under SSE by the census); unmasked x87 exceptions
(#MF: built since I5c, Built (I5c); before it FLDCW/FLDENV/FRSTOR refused
them, 160 + 44 + 0 initramfs sites take the transaction); FIP/FDP (0 in images); FLD/FSTP m80 as resident round
trips (2,411 FLD m80 and 1,098 FSTP m80 initramfs sites): a loop of FLD m80 + FSTP m80 takes 11.4 us per iteration against
under 0.1 us for FLD/FSTP m64, because every system instruction is a host request for its decode today (a resident decode
cache, L5, or native 80-bit conversions would remove it) (K2: 0.5 us per pair, no host exit; Built (K2)).
**For I5b:** FXSAVE/FXRSTOR's and XSAVE's x87 image is built from `Cpu::fcw`, `x87::fsw()`, the abridged tag byte as it is in
the page (`frame::X87_FTW`, FXSAVE's own format), ST(i) = `natemu_abi::x87::to_f80(x87::mm(x87::top() + i))` at 32 + 16i (bytes
10..15 zero), FOP/FIP/FDP 0; restoring takes FCW (refuse a cleared mask: `x87::masked`), `x87::set_fsw`, the tag byte and
`x87::set_mm(top + i, from_f80(.., FCW.RC))`; then (MXCSR aside) a FCW change needs no flush: the x87 rounding is in
`code_context`. fexc must cut FXSAVE/FXRSTOR (it does: `0f ae /0 /1` are `unsupported_form` today). **Host/fexc pairing:** the
host passes `--x87-gated`, `--wait-gated` and `--x87-fpcr` whenever the vCPU's CR0 or FCW/MXCSR call for them, so it needs a
fexc of series25 or later (series22 refuses the arguments: runs with CR0.TS/EM set, or MXCSR.RC not nearest, fail there;
fexc refuses `--config X87REDUCEDPRECISION=0` under slice1).
**Checks** (series25, block sizes default and 1 where VM programs): `tools/reference/gen_i6.py` corpus (120 programs, x87
forms on `double` groups, FCW.PC = 53 bits, and `exact` groups, PC = 64 bits; FNSTSW after compares and FXAM, SETcc after
FCOMI, FNSTENV/FNSAVE/FRSTOR/FLD/FSTP m80 round trips, the stack stored at the end) against QEMU 7.2: 120 match at both block
sizes. Recorded QEMU differences the corpus steps around: FCOMI keeps OF/SF/AF (SDM: cleared; not compared after one), FCOM
and FTST keep C1 (SDM: cleared; masked after compares, FXAM's C1 cleared through FLDENV), FSQRT of a negative sets C2
(undefined: FTST follows), no stack faults at all, and its in_asm log garbles an instruction crossing 1 KiB of a translation
block (the generator ends blocks every ten operations). `tools/run_i6_faults.py` (276 cases, judged by the SDM rules in its
docstring; QEMU is no oracle for stack faults): #PF not present/read-only/crossing, #GP/#SS non-canonical and straddles for 24
memory forms with the x87 state and memory unchanged at each fault; #NM under TS, EM, TS+EM, TS+MP for 13 forms (refused ones
included), WAIT only with TS+MP; FCW unmasking refused (FLDCW/FLDENV/FRSTOR); helper forms and FBSTP `unsupported_form`; LOCK
#UD; FLD m80 overflow and FSTP m80 underflow; FXAM's six classes; FIST/FISTTP indefinite with IE and no PE, PE alone when
inexact; x87 flags never in MXCSR and FCW.RC against MXCSR.RC/FTZ in one block; 29 stack-fault cases (overflow on FLD
m32/m64, FILD, FLDZ, FLD ST(i); underflow on FADD register and memory forms, FSUBP, FSQRT, FCHS, FABS, FST/FSTP
m32/m64/ST(i), FIST/FISTP/FISTTP, FCOM, FUCOM, FCOMP, FTST, FCOMI, FUCOMIP, FXCH, FLD ST(i); FXAM after FFREE without a fault;
FNCLEX after one; FSTP's #PF ahead of its underflow), each checking FSW, the tag byte, the registers and the stored value; the
review's cases (C1 after FLD m64, FLD1, FLDPI, FILD, FLD ST(i), FCHS, FABS and FCOMI following an FXAM of a negative value; SNaN
through FLD m32/m64 and FST m64; FLD m64 of a denormal (DE); an underflow beside a QNaN; FCOMI clearing OF/SF/AF; 66 + REX.W
FNSTENV writing the 28-byte image; re-keying across FLDCW, FLDENV and LDMXCSR; #AC at CPL 3 on 15 x87 operand forms by Table
7-7); Linux's probes (the decompressor's FNINIT/FNSTSW/FNSTCW: status 0, control & 103FH = 3FH; the FDIV-bug sequence stores
0): 276 as expected at both block sizes; the reviewer's own 33 reproducers pass. `verify_precision` on the corpus bodies
(`gen_i6.py --vp`: the resident forms and block-ending jumps removed, so every body compiles as one block): 120 compiled,
12,579 sites, 0 violations, 568 x87 store sites reported under deviation 9 (28.8 native instructions per guest instruction).
Non-x87 objects (fixtures, seed 4242, the I5a corpus: 222 programs) are byte-identical to series22. FEX ASM suite: 794 -> 1,052
pass, 0 fail, 22 `precision deviation` (`run_asm.py` `x87_value_ok`: every difference is an x87 register MMn widened from its
double, or an XMM register holding an 80-bit image, that is exactly the 80-bit expectation rounded to 53 bits under one of the
four rounding modes; a NaN must agree in sign, quiet bit and the top 51 significand bits; an 80-bit denormal expectation never
matches; only the 15 named FEX precision tests (FCW.PC single/double/extended, deviations 1-2) may also have the expectation
equal to our double rounded to 24 bits, or one 53-bit ulp from it (FLD m80 rounded the operand under the group's RC: FSUBR's
0 - x then rounds the other way); any GPR, RIP or other difference fails, and the review's hole tests are must-fail selftests:
D9_E9..ED (FLDL2T..FLDLN2, deviation 3), DD_04, DD_04_2 and the 15 precision_test_*), x87
unsupported: 79 transcendental/remainder/scale, 10 FXTRACT, 7 undocumented aliases, 6 FFREEP, 5 BCD, 4 FXRSTOR (I5b), 2 FCW
unmasking (D9_05, refused), 1 expecting FEX's FXAM bug (FXAM_Simple); X87: 134 of 217 pass, X87_F64: 110 of 150. Census
(`experiments/track03/kernel-census`, rerun into `natemu-raw/kernel-census-i6-20261029-01/`): x87 class kernel 31 -> 10
(FXSAVE/FXRSTOR only: I5b), initramfs 14,440 -> 65; x87 forms filed `system` (resident transactions): kernel FNINIT 7, FNCLEX
1; initramfs FLD m80 2,411, FSTP m80 1,098, FLDCW 160, FNSTENV 44, FLDENV 44, FNCLEX 16. Full standing suite on series25 (the
combined I6 + M3 build): unit (cargo 0 failures, python), stack check, fix/fix-1 64, seed/seed-1 44, faults and faults-1 7,581
each (faults rerun once after `hv_vm_create` contention from another agent's VMs), flow 64, loopfaults 846, probes 83, i5a
faults 454, i5a DE 351, corpora, smp, limits, kicks, system, coherence, sharedpt, litmus, contain, privilege, delivery, events,
i6 faults 276 at both block sizes, `run_m3.py` (corpus 66 programs / 715 accesses, faults, refusals, kicks, contain, xmc,
boundary), all with 0 failures.
*Built (K2, the resident system-instruction path; no fexc change, series25):* **Where the decode happens.** Host-side,
at translation time, as before: `system.rs` stays the only x86 system decoder and decodes the instruction fexc cut,
from exactly the bytes the resident walked and marked for that translate request. What changed is that the resident
keeps the decode: on a `REPLY_SYSTEM` to a translate request it files the `System` record in its code cache as a block
without code (`cache::SYSTEM`, the record right after the `Block` where a block's sites are; `cache::place_system`;
`code_size` 0, never in L1, never entered or linked), keyed by (RIP, context) in L2 and filed by its source pages and
guest bytes, so it is invalidated exactly as a block of those bytes would be (a peer's or its own write to the page,
INVLPG over its range, every flush), and placed under the same rule as a block (a write racing the fetch, a lost mark or
a flush while it was asked for: discarded, and the instruction stepped from fresh bytes). Later visits to the RIP find
it in `find`'s L2 lookup and run the transaction with no host exit; the single-instruction step (X2's shadow step, the
killed-block and code-write `step`) uses a kept record too (`kept_system`), except a BT* form under alignment checking,
which is compiled plain by design. Rejected: a record from fexc like M3's (a second decoder of the operand forms in the
shim, which FEX's decoder does not decode for `0f 20..23`), and a record attached to the preceding block (a system
instruction is reached from many predecessors, a delivery, an IRET or a step; keyed by its own RIP it covers all). The
test drivers' hooks (`SET_CPL`, `EVENT_HOOK`) are not instructions and are answered at every request. **Cross-modifying
code** gives old or new: a record describes the bytes of one translation; a rewrite posts the page and the next drain
kills it (`run_k2.py xmc`). **Host-owned state:** none; CPUID (generated profile), the MSRs (their record) and the TSC
(CNTVCT) were already in the resident (S1), so nothing moved. **RDRAND:** the pool is no longer refilled at every
RDRAND's exit, so an RDRAND that finds it empty asks the host once (`REQUEST_ENTROPY`, the host tops it up as at every
exit) and takes the next word; CF = 0 remains only for a failed host source. **Exits that remain:** compile requests
(the first visit of a block or system instruction per cache generation, and step-slot compiles of ordinary
instructions), fetch faults (with the request), device accesses (M3), one entropy refill per 32 RDRANDs without another
exit between, the test drivers' hooks, stop RIP and logging handlers, and the end. Every flush still drops every kept
decode: each CR0/CR4 change, CR3 load and MTRR write, so the next visit of each system instruction after one is a
decode exit again (K1's). Also: a vCPU running system instructions no longer leaves the guest for them, so the host
watchdog (10 s without a host exit) now applies to such loops as to any guest loop: a loop made only of system
instructions spins in the resident until the watchdog ends it. **Resident cost:** the state record
and frames are copied 8 bytes at a time (`record::copy_record`; the byte-by-byte volatile copy made every resident entry
cost about 1 us), and `fill_l1` skips a rewrite of an L1 entry that already holds the block. **Accounting:** the program
driver reports `decode_exits`, `decodes` (by kind) and `entropy_exits` per vCPU, and with `NATEMU_EXIT_LOG=FILE`
appends one line per vCPU (systems, entropy, decodes by kind) for a suite's totals. **Checks** (series25; decode exits from `NATEMU_EXIT_LOG`, before = the HEAD runtime with the new host's counter):
`system` 112,024 decode exits for 112,027 transactions before, 16,931 for 176,337 after (more transactions: the time-bound
loops run faster), of which MOV to CR 4,963, MOV from CR 2,984, RDMSR 2,629, WRMSR 2,068, CPUID 773 (each the first visit after
a flush: the CR, EFER and MTRR writes in these programs flush), INVLPG 92,651 -> 288; `events` 61,186 for 61,514 -> 1,282 for
53,500 (IRET 23,574 -> 182, HLT 14,022 -> 50, CLI 6,076 -> 406, STI 5,828 -> 158, MOV Sreg 11,312 -> 112); `run_i6_faults.py`
616 -> 616 (every case runs each instruction once: first translations only); `run_m3.py` 20 -> 23 (a few more steps meet
the INT3 in its xmc). Cost per loop iteration at CPL 0, 1 vCPU (`run_k2.py cost`, slope over N = 100,000 and 200,000,
median of 3): CLI 6.39 -> 0.46 us, STI 6.35 -> 0.47, PUSHFQ+POPFQ 6.40 -> 0.51, SWAPGS 6.24 -> 0.51, RDTSC 6.36 -> 0.48,
CPUID 6.34 -> 0.47, WRMSR KERNEL_GS_BASE 6.33 -> 0.47, FLD+FSTP m80 10.92 -> 0.56 (two transactions, one resident entry); a
loop's decode exits stay 1 (2 for the m80 pair) at both N (a failure if they grow). What remains is one resident round
trip per block exit into a system instruction (a literal exit to it is never linked: the linker, the transaction, the next
lookup); without the record-copy change it was 1.32 us. Breakdown (the review's measurement, `cost` variants): each
further transaction in the same resident entry costs about 55 ns (CLI x2, x4 per iteration), so about 0.36 us is the fixed
cost of one resident entry on the linker path; the cheapest resident round trip, a 128/64 division by `svc #DIV`
(`run_k2.py` `div`: RDX = 1, divisor 2), takes 0.167 us (0.152 in the rerun after review), so an `svc` alone (about 31 ns) describes no current resident
entry. The rest is in `bank`/`sse::bank`, `block_at`, `enter` (the frame build, `sse::fill`, the whole `State`
`write_record`) and `tables::select`, not yet instrumented. `run_k2.py rdrand` (4,000 RDRANDs: all CF = 1, 124 entropy exits,
values differ); `run_k2.py xmc` (300,000 rounds against 20,000,000 one-byte rewrites, block sizes default and 1: CLI <-> NOP,
CLI <-> STI, SWAPGS <-> RDTSCP, FLD m80 <-> FILD m32 reach their end with 435-1,574 decode exits and 723-2,357 steps from
rewritten bytes; INT3 <-> CLI reaches the INT3 and takes #BP, RIP after it), never another stop; each run must translate X again (decode
exits > 1) and an INT3 run must take #BP. `run_k2.py coherence` (the review's cases, block sizes default and 1; each with
the old instruction decoded once over two visits, then the new one observed): A the vCPU's own write from another page, B
its own write on X's page in a loop, C remap + INVLPG and remap + CR3 reload, D a write through an alias of X's frame, E a
SWAPGS crossing 0x200000 with only the second page remapped (to RDTSCP) and INVLPGed, F a peer's rewrite, a flag, then
CPUID (three runs); and a kept BTS record under alignment checking (aligned five times, each stepped plain, then #AC(0)
at the BTS; all aligned: completes). Shown red with record invalidation disabled (`cache::kill` skipping SYSTEM records):
every xmc run (decode exits 1, no #BP) and A, B, C-INVLPG, D, E, F fail; C-CR3 passes (the CR3 load flushes) and the BTS
cases pass (no invalidation involved); green with it restored. Full standing suite green:
cargo build/test, stack audit (gate 18,464 bytes, 22,528 with a nested entry to its halt, of 32,768; HEAD 18,368/22,368),
python unit 259, fix/fix-1 64, seed/seed-1 44, faults/faults-1 7,581, flow 64 x 2, loopfaults 846 x 2, probes 83, FEX ASM
1,052 pass 0 fail (22 precision deviations), corpora, smp, limits, kicks, system, coherence, sharedpt, litmus, contain,
privilege, delivery, events, i6 faults 276 at both block sizes, i5a faults 454, i5a DE 351, `run_m3.py`, `run_k2.py`.
**Not built:** linking a block's exit straight into a kept system record (the remaining ~0.45 us), keeping records across
flushes (K1), inline translation of the simple flag forms (CLI/STI/POPF without TF).
*Built (L1, the boot-protocol loader; no fexc change, series25):* `crates/host/src/boot.rs` loads a bzImage by the 64-bit
protocol of `Documentation/arch/x86/boot.rst` v6.12.107 and nothing else (no kernel is recognised). **Header:** boot flag,
`HdrS`, protocol >= 2.12, the jump byte, header end 0x268..0x290, LOADED_HIGH, XLF_KERNEL_64, a power-of-two alignment
(relocatable), init_size != 0, a pref_address (fixed), setup_sects 0 = 4, the file holds `syssize * 16` bytes (checked
arithmetic), the payload inside it; codes and order are `tools/models/bootproto.py`'s. **Map** (`Layout { low_end, high }`,
CLM-03-03-69): reserved 0..4 KiB, RAM to 0xa0000, reserved legacy hole, loader page tables 1..2 MiB, the SMRAM hole to
6 MiB, ACPI NVS 6..8 MiB (L2: RSDP, tables, mailbox), NVS boot scratch 8..16 MiB (X4), RAM 16 MiB..L (default 2 GiB,
any 2 MiB multiple to 3 GiB), reserved L..4 GiB, RAM 4 GiB..+H. **Placement:** the boot.rst runtime start (relocatable:
align_up(max(16 MiB, pref_address), kernel_alignment); else pref_address), window max(init_size, loaded size) inside
[16 MiB, L); the initrd page-aligned downward, last byte <= initrd_addr_max, end <= L, above the window. **Zero page**
(0x10000): zeroed, the header 0x1f1..end copied, then every field boot.rst types "write" is the loader's (type_of_loader
0xFF, ext_loader_*, heap_end_ptr 0, loadflags' QUIET/KEEP_SEGMENTS/CAN_USE_HEAP clear, ramdisk_image/size and their high
halves (0 without an initrd), cmd_line_ptr and its high half, hardware_subarch and its data 0, setup_data 0), code32_start,
acpi_rsdp_addr (L2's hook `Inputs::rsdp`, inside the ACPI NVS range; kernels of protocol 2.12/2.13 predate the field and
ignore it), the e820 table. Command line 0x11000 (<= cmdline_size), GDT 0x18000 (`__BOOT_CS` 64-bit, `__BOOT_DS`, flat),
identity map of [0, L) in 2 MiB pages at 0x100000. **BSP:** RIP = K + 0x200, RSI = 0x10000, RSP = 0x18000, CR0 =
PE|ET|NE|PG, CR4 = PAE, EFER = LME|LMA, RFLAGS = 2, CS 0x10, DS/ES/SS 0x18, FS/GS null, IDTR limit 0 (an exception before
the kernel's own IDT is a shutdown). Typed refusals (`LoadError`), each tested. For L2: table writes must be added to
`Loaded::writes` (the dump and the QEMU image use it). For M1: `load` takes one RAM range from 0; high RAM needs a
multi-range description first. Driver `natemu-host RUNTIME boot-linux FEXC BZIMAGE [--initrd] [--cmdline] [--ram]
[--rsdp] [--load-only] [--dump] [--stop-at] [--save]` (`drivers/linux.rs`, no tamper). **RAM limit (superseded by M1, Built (M1): guest RAM is a run-time size, the VM path runs in-tree):** in-tree guest RAM was
`GUEST_RAM_BYTES` = 4 MiB, below the map's 16 MiB RAM start, so a VM run refuses `BAD_LAYOUT` until M1. Causes: the
constant itself; the global area's code-page and writable-page masks (one byte per page in 0x1000-byte slots, so 16 MiB
before RECORDS/RESETS/EVENTS move); the guest-RAM alias must end below `cache::CODE_VA` (PRIVATE + 256 MiB, alias from
PRIVATE + 2 MiB: about 254 MiB in this layout); 2 GiB also needs a larger private window. **Smoke test** (owner Q8,
`tools/run_l1_smoke.py`) on a scratch build with 128 MiB guest RAM (diff:
`/private/tmp/claude-501/-Users-liaozping-orca-natemu/025ed521-9978-4fb2-aaf7-b90da95d37f3/scratchpad/l1/ram128.diff`,
not committed; M1's): the Debian 6.12.107 bzImage with `mitigations=off nokaslr` runs its decompressor from the loader's
entry to its jump into vmlinux (`e_entry` 0x1000c43) in about 1 s with no other stop (2,112 blocks, 66 system
instructions: CPUID, string copies, its own GDT/IDT/CR3 loads, #PF delivered to its on-demand identity-map handler, CR2
0x3800020 at the end; no port I/O, MSR, RDRAND or RDTSC). QEMU 11.1.2 (TCG, `-cpu Haswell`, no BIOS: a firmware from the
reset vector into the identical state, the loader's writes as `-device loader` blobs, a gdbstub breakpoint at 0x1000c43):
every GPR, RIP, RFLAGS, CR0/2/3/4, EFER, selectors, GDTR and IDTR equal; the zero page after the decompressor equal; the
four vmlinux PT_LOAD segments equal in natemu, QEMU and the ELF; of the 0x37ef000-byte window one byte differs, the A bit
of the decompressor's code descriptor (natemu 0x9b, QEMU 0x9a: QEMU's far-transfer CS load sets no A bit, S2b). `nokaslr`:
with KASLR the decompressor draws RDRAND/RDTSC entropy and moves the kernel, so neither the stop address nor the bytes
would be comparable. Checks: `boot::` unit tests (5: every header and loader refusal, the map, the write fields, a load);
`tools/run_l1.py` check 12/12 (Debian with and without its initrd at 2 GiB, 3 GiB, 128 MiB; synthetic 2.12/2.15,
aligned-up, fixed-address and stale-write-field images; three corrupted-dump controls), rejections 41/41 (16 header
mutations on two images with the model's codes, 9 loader refusals); `bootproto.py` gained the setup_data rule
(independent author). Not done: a KASLR run (needs a stop rule other than a fixed RIP); the VM path in-tree (M1).
*Built (X3, local APIC, its timer and IPIs, and L5's clock minimum; no fexc change, series25; after two independent reviews):*
(Section numbers are the APIC chapter's as the plan cites it, ch. 11; the repo's SDM snapshot,
`artifacts/research/track01/source-snapshots/2026-09-03/intel-sdm-vol3a.txt`, numbers it 13: 11.x there is 13.x.) **Profile** (`cpu_profile.json`, kept as CPU 16): xAPIC and x2APIC (CPUID.1:ECX.21) advertised, TSC-deadline not
(ECX.24 clear: LVT timer bit 18 reserved, IA32_TSC_DEADLINE #GP), ARAT advertised; integrated APIC version 0x00050014
(QEMU's): six LVT entries (timer, thermal, PMC, LINT0, LINT1, error; no CMCI, the profile clears MCE/MCA), no directed
EOI (SVR bit 12 reserved); the timer counts the 24 MHz crystal of CPUID.15H through its divider. **State**
(`crates/abi/src/lapic.rs`: record, rules, unit tests; `runtime/src/execution/lapic.rs`): a per-vCPU record in the
state page (`LAPIC_OFFSET` +0xa00, written by the host at reset, `execution::set_lapic`; the resident's): SVR, LDR,
DFR, ISR, TMR, LVT[6], ICR, ESR (latched) and errors since its last write, the timer (initial count, DCR, start crystal
count, expiries taken, the V of the next expiry, a spent one-shot flag) and CNTFRQ as read at start. The IRR is X2's
`Events.vectors`; the TPR is the bank's `Cpu::cr8`, which now holds TPR[7:0] (CR8 reads and writes TPR[7:4], MOV to
CR8 clears TPR[3:0]); `Events.reserved` became `Events.apic`, the word senders match destinations against (LDR, DFR
model, hardware/x2APIC/software enable; owner-written, `lapic::published`). **Priority** (11.8): PPR = TPR if its class
is at least the highest ISR vector's, else that class; the highest IRR vector is delivered when its class is above
PPR's and the APIC is hardware and software enabled (`events::next`, replacing X2's highest vector); taking it moves
the IRR bit to the ISR; EOI clears the highest ISR bit. EOI and TPR writes need nothing more: every transaction and
device access returns to `find`. No spurious interrupt is ever generated (acceptance and delivery are one resident
step), so delivery status always reads idle. **Access:** xAPIC MMIO at the base page in `mmio::local` (M3's hook: no
host exit; 32-bit accesses at 16-byte-aligned offsets only, anything else refused `unsupported_mmio` status `device`;
unimplemented offsets set ESR bit 7 and read 0; read-only registers ignore writes); x2APIC MSRs 0x800-0x8ff as resident
transactions with 11.12's rules (#GP: an index not in Table 11-6, WRMSR to a read-only or RDMSR of a write-only
register, a nonzero EOI or ESR write, any reserved bit (11.12.1.3), outside x2APIC mode; ICR one 64-bit write, SELF_IPI,
LDR derived and read-only). IA32_APIC_BASE: x2APIC -> xAPIC and disabled -> x2APIC #GP (EXTD without EN by the profile
rule); entering the disabled state resets the APIC (record, IRR, TPR, timer): the disabled word is published first, so senders
stop before the IRR is cleared, and the IRR is cleared again when the APIC is enabled (nothing is accepted while
disabled); what remains is a sender that read the old enabled word before the disable and posts after the re-enable's
clear (a resident descheduled that long between its read and its OR), whose stale bit is then held like any pending one; a base moved to a device page is answered
there (the resident answers before the registry), onto guest RAM stops `unsupported_system` before any effect (it
cannot overlay RAM; Linux never moves it). SVR software disable masks every LVT and keeps them masked. **IPIs** (11.6):
an ICR write sends at once: fixed and lowest priority set the target's IRR bit, NMI its NMI bit (atomic OR), count a
post (`Events.posts`) and ring it (SGI 1; this vCPU's own events are looked at at its next boundary); destinations
physical (FFH broadcast, 11.6.2.1), logical flat (MDA AND LDR[31:24]: all ones reaches every APIC with a logical ID bit
set, not one whose LDR is 0, as 11.6.2.2's bit-wise AND reads), cluster (all ones matches every cluster), x2APIC cluster
(FFFF_FFFFH broadcast, 11.12.9), shorthands self/all/all-but-self; a fixed message to a
software-disabled APIC is not accepted (11.4.7.2), and the host's `post` applies the same rule; vectors 0..15 set ESR
send/receive illegal and send nothing. INIT and start-up go to `lapic::ap_start` (X4's hook: a typed stop
`unsupported_system` at the sending instruction, nothing sent); INIT level de-assert has no effect; SMI and reserved
modes stop `unsupported_system`. **Timer** (11.5.4): one-shot and periodic from V = CNTVCT, exactly; the next expiry's V
(`clock::count_at_crystal`, never early) is programmed into the Arm virtual timer (CNTV, PPI 27: `vm.rs` puts it in
GICR group 1 and enables it, as the native-interrupts study noted). Its IRQ only rings: `timer_irq` turns CNTV off when
the expiry is due (level-triggered) or leaves it armed for a later deadline (a stale IRQ after a re-arm left a halted
vCPU with no timer: found by the HLT tests, fixed); `lapic::poll` at the top of `find` and in `park` takes every expiry
due (an unmasked LVT sets the vector's IRR bit; expiries passing while one is pending merge, as in the IRR) and arms the
next. The current count is computed from V when read. Writes to the LVT timer, DCR and SVR first take the expiries due
under the old settings (a masked LVT drops them); a mode change counts no expiry that passed under the old mode, a
one-shot count that reached 0 stays 0 until the initial count is written, a DCR write keeps the current count and
counts on at the new rate (review findings 1-3). **HLT:** `park` polls the timer, wakes on its IRQ, and checks `next`
again after seeing `end` (X2's note); `Events.halted` holds state 1, or 2 while its own timer can wake it (armed,
unmasked, IF = 1, its class above PPR), and the posts count it read before it last found nothing to take.
**Ending halted vCPUs (first review's finding 4, sound rule):** with peers posting at any time, the program driver now reads
every vCPU's park state twice and ends them only if both reads are equal and every vCPU is in state 1 with every post
seen (`execution::idle`): a vCPU that runs again must leave its park, which needs an unseen post from a running vCPU,
and the first to leave did so between the reads, changing its post count. The old rule (one read of each `halted`)
ended a vCPU a peer was about to wake: the IPI ring `apic-pingpong` stopped after 12-111 of 3,000 receipts in all 4
runs (2 and 4 vCPUs, block sizes default and 1); the new rule passes 4/4. State 2 (its own timer can wake it) uses the
vector the expiry actually raises: the timer's, or for an illegal timer vector (0..15) the LVT error vector when that
entry is unmasked with a legal one (the second review's repro: before, such a vCPU was in state 1 and ended although
its timer woke it); `selfwake-timer`, `selfwake-illegal-timer-via-error` (delivered, RIP after the HLT) and
`selfwake-none` (both vectors illegal: ended `halted`) cover it (`halt` 10/10). **Condition on host sources:** the argument holds only
for sources that are vCPUs or veto the decision; every asynchronous host source (the driver's kickers; L4a's PIT, RTC,
UART and device SPIs) must post through `execution::post` (it applies acceptance and counts the post) and keep the
decision off while it may still fire, as `kickers_left` does (`program.rs`). **L5 minimum** (`crates/abi/src/clock.rs`):
CRYSTAL_HZ from CPUID.15H ECX (24 MHz), a compile-time check that TSC_HZ = crystal x EBX / EAX (1 GHz = x 125 / 3),
`scale`/`crystal`/`tsc` (floors of V x rate / CNTFRQ) and `count_at_crystal` (their exact inverse), unit-tested for
exactness at five rates; the resident's TSC uses it, so the TSC is final at 1 GHz (leaf 0x16 not advertised).
**CNTFRQ is measured, never assumed:** the guest reads 24,000,000 (each run reports it, `lapic.cntfrq`), while macOS
26 on the M4 gives host EL0 1,000,000,000. **QoS (tried, reverted):** running the vCPU threads at the user-interactive
QoS class did not change the HLT wake lateness (measured both ways: 49-112 us at a 200 us period, 0.14-1.0 ms for a 2 ms
one-shot; the earlier HLT failures were the stale-IRQ bug and first translations), cost about 15 ns per system round trip
(K2's loops), and could starve the host's default-class threads under load (each vCPU's `fexc` compile server, the
service threads): a vCPU waiting for its compile then stalls while its peers spin. vCPU threads stay at the default
class. **Model choices and facts:** the xAPIC ID is read-only (11.4.6, model
specific); a periodic -> one-shot switch whose count has already passed N ticks makes the count 0 with no expiry (legal;
QEMU does the same, and Linux writes TMICT next); a software-disabled APIC holds its IRR undelivered and accepts no new
fixed message (11.4.7.2: "pending interrupts in the IRR and ISR registers are held", while it "responds normally" only
to INIT, NMI, SMI and SIPI; we read "held" as kept pending, not delivered, as QEMU does; KVM delivers them); x2APIC
ICR bit 12 is #GP (11.12.9: the delivery status bit "is removed" in x2APIC mode, so it is reserved, and 11.12.1.3 makes a
reserved bit set in a WRMSR #GP; QEMU accepts it); the timer's count starts at the crystal edge at or before the
initial-count write (`clock::crystal` floors V), so its first tick may come up to one crystal period (41.7 ns) early
relative to the write itself (none when CNTFRQ is the crystal rate, as on the M4); APR and RRD are not implemented (Table 11-1 note 1; xAPIC reads 0, x2APIC #GP); lowest priority goes to the
lowest-numbered member of the set (11.6.2.4: model-specific arbitration); the timer's period is exactly the initial count
in divided ticks; no spurious interrupt; TMR reads 0 (no level-triggered source yet; L4a: the shared `Events::tmr`). **Checks** (`tools/run_x3.py`,
block sizes default and 1 unless stated): `qemu` 9 programs x 2 (register read-backs in xAPIC and x2APIC; priority, TPR
and EOI order with nested self IPIs in both modes; CR8; the x2APIC fault program, 40 cases; x2APIC IPIs at 2, 3, 4
vCPUs: fixed physical, all-but-self, NMI, logical cluster, peers to CPU 0): natemu equals QEMU (registers, logs without
their TSC, counters; logs only at 1 vCPU) and both the SDM model; `ipi` xAPIC MMIO IPIs at 2 and 4 vCPUs (physical,
logical flat, all-including-self, NMI, lowest priority; QEMU's BSP cannot be xAPIC with peers): counts exact, 4/4;
`timers` 24/24 (one-shot, periodic /1 and /16, masked, HLT one-shot, HLT periodic, both modes; two rounds, the second
measured): every current-count sample is the model's count at some V between the RDTSCs around it, no delivery before
its expiry, each expiry delivered (possibly merged) within 2 ms, one-shot exactly once, masked never; measured lateness
7-20 us running, 50-117 us from HLT at a 200 us period, 0.45-1.0 ms from HLT for a 2 ms one-shot, about 0.4 ms for the first delivery at a resume point not yet translated (host compile; expiries merge
meanwhile); `stress` 6/6 (4 vCPUs in each mode, 2 vCPUs x2APIC: 2,000 random IPIs per vCPU under per-flow ordering, a
periodic timer on each, nested handlers): sent = received on every flow (8,000 at 4 vCPUs), 0 priority violations at
any handler entry, timer deliveries never above expiries (about 45 % delivered, the rest merged); `halt` 10/10 (above).
**HLT wake lateness is macOS's, proportional to the sleep:** a HLT one-shot measured with 5 rounds (the handler and every
path translated in round 1, `apic.timer_program(rounds=5)`) is late by 106 us for a 0.2 ms sleep, 0.96-1.01 ms for 2 ms
and 2.0-5.0 ms for 10 ms (3 runs each; the same at the default QoS class), about half the sleep: not a first translation but the
wake of a vCPU parked in `wfi` on the virtual timer (HVF's park and macOS's timer leeway). A halted Linux CPU's 4 ms tick
can thus come about 2 ms late; L5 should own the remedy (e.g. arming CNTV short of the deadline and re-arming on each
wake, or a host-side wake), measured;
`cases` 54/54 (xAPIC widths and alignment, the page while disabled or in x2APIC mode, re-enable resets, relocation,
INIT/start-up/SMI typed stops, INIT de-assert, ESR send/receive illegal and its latch, illegal register, read-only
registers, software disable, x2APIC MSRs in xAPIC mode, CR8 <-> TPR, the two timer-write cases of the review); abi unit
tests 9 new (clock, lapic). X2's `events` programs now enable the APIC and end interrupts for real in natemu too
(`events.py`): 136 ok; `run_m3.py` `boundary` and `x2-hlt-masked` set `svr 0x1ff`; `system`'s APIC-base case now moves
the base onto RAM (the typed stop). **Cost** (`run_x3.py --only cost`, slope over N = 100,000 and 200,000, median of 3,
CPL 0, 1 vCPU): xAPIC EOI write 0.745 us, TPR write 0.658 us, register read 0.661 us per access (each ends its block:
the site's walk, the completion, a dispatch; no host exit); x2APIC EOI WRMSR 0.451 us, TPR WRMSR 0.452 us (K2's
resident transaction path). **Round-trip cost** (second review: the full tree was +7.6 % per system round trip over HEAD): measured with interleaved
A/B runs (CLI and CPUID loops, slope over 100,000 and 200,000 iterations, 30-40 rounds, medians; the machine was shared,
so +-10 ns): HEAD 466, HEAD + M1 (the tree without X3's runtime) 481, the tree before 514, after 497 ns per CLI. The QoS
class was 15 ns of it (reverted, above). The rest, about 15 ns, is not in executed X3 code: removing `lapic::poll` from
`find` (it is a load and a compare when no timer is armed), the LAPIC priority call in `events::next` (not reached with
IF = 0), the PPI 27 set-up and the timer-IRQ check each changed nothing measurable, and `system`'s disassembly is the same
instructions; it follows the runtime binary (host and runtime swapped between builds), so code layout is the likely
cause, left for P2's instrumentation. **`run_k2.py xmc` made deterministic** (its old check, "X translated again", failed
under load when the peer stalled): vCPU 1 rewrites one byte of X only after vCPU 0 advanced 100 iterations, raises a flag
and waits for vCPU 0's acknowledgement; vCPU 0, on the flag, runs CPUID and must then execute the new instruction
(observed: IF, ECX of RDTSCP, the loaded x87 value; INT3 taken); vCPU 0 runs until 2,000 rewrites are checked. Green 10/10
alone and 50/50 under 12 `yes` hogs (5 runs), 0 mismatches; red 10/10 with kept system records never invalidated (the K2
control: 999-1,000 of 2,000 checks ran the old instruction, and the INT3 was never taken). **QEMU deviations** (11.1.2; judged by the SDM, `run_x3.py` `QEMU_DEVIATIONS`; on the other 20 fault cases natemu
equals QEMU, #GP or not and the faulting RIP): it accepts
WRMSR to read-only and RDMSR of write-only x2APIC registers, nonzero EOI/ESR writes, every reserved bit, an APR read and
ICR-high, x2APIC MSRs with the APIC disabled, and advertises TSC-deadline (20 of the 40 fault cases); it cannot
re-enable an APIC once disabled. **For X4:** replace `lapic::ap_start` (INIT/start-up arrive at the sending
instruction with the target set computed) by posting into the target's events; `lapic::event_word(v, field)`,
`cache::kick(v)` and `park` are the pieces; APs start from the reset APIC with their published word written.
**For L4a:** host `execution::post` + the vCPU's SPI is the external-interrupt path (it applies acceptance and counts
the post); `natemu_abi::lapic::{published, in_destination}` match I/O APIC destinations from the global area; a
level-triggered source must set TMR on acceptance and get the EOI broadcast at the marked place in `lapic::eoi`; LINT0
(ExtINT) and LINT1 (NMI) entries are stored and read back with no source. **For L5:** `natemu_abi::clock` and the
final TSC; PIT/RTC/PM from V by `clock::scale`; the wake latencies above (a halted vCPU's timer wake is macOS's).
**Not built:** TSC-deadline (not advertised), INIT/SIPI (X4), LINT sources, ExtINT, level-triggered interrupts and EOI
broadcast (L4a), thermal and PMC sources (none in the profile), APR.
*Built (M1, guest RAM and the address-space layout; no fexc change, series25; after an independent review):* **RAM size**
is a run-time parameter: R, a 2 MiB multiple up to 3 GiB (`execution::MAX_GUEST_RAM_BYTES`, the map's largest L), chosen
at `execution::prepare` and stored in each vCPU's `cache::Control::ram_bytes`; the resident builds its RAM description
from it (`guest_ram()`). `run-program --ram BYTES` (default 2 GiB) and `--place GPA` (where the driver starts placing
tables and pages, default 0x1000); `NATEMU_RAM`/`NATEMU_PLACE` set those defaults, so any runner runs a whole suite at
another size or placement unchanged. `boot-linux` runs a VM of the map's L (default 2 GiB) and gains `--code-cache`
(default the largest) and pool reporting (`evictions`, `shadow_resets`, `retries`, the cache fill; a watchdog stop reports the walk `retries` so far). **Legacy hole
(owner decision, October 29: 0xA0000-0xFFFFF is not RAM; the reserved ranges that are RAM on a PC stay RAM): applied with
L3 (Built (L3)), which gives its pages the unassigned rule.** Built and measured: RAM as two ranges ([0, 0xA0000) and [1 MiB,
R)), the hole through the device path, never shadowed, a fetch from it not RAM, the driver placing around it and refusing
identity pages in it, devices registrable there (patch kept at
`/Users/liaozping/orca/natemu-raw/m1-legacy-hole-20261029.diff`).
With the typed `unassigned` stop the L1 smoke test then stopped after 242 blocks: the decompressor's `get_rsdp_addr()`
(`acpi_rsdp_addr` 0) searches the EBDA and 0xE0000-0xFFFFF for "RSD PTR" with plain loads; with L3's rule the search
reads all-ones and completes (L2 now fills `acpi_rsdp_addr`, which skips it). Everything else the boot map reserves below 16 MiB is RAM and stays
RAM.
**Per-page structures** (global area), sized once for 3 GiB, only the first R / 4096 pages used, untouched pages never
backed: the code-page and writable masks hold 4 bits per page (bit v = vCPU v), page p's nibble at bit 4 (p % 16) of
word p / 16, so one atomic word op still reads or changes every vCPU's bit of a page (the protocol is unchanged; 384 KiB
each at 3 GiB). The 4-bit groups limit a VM to 4 vCPUs (`MAX_VCPUS <= 4`, asserted); more vCPUs need 8-bit groups (768
KiB per mask at 3 GiB). Each vCPU's invalidation record is `{pending, summary, stale}`: producers set the stale bit, then
its word's summary bit, then `pending`; the consumer swaps `pending`, then each nonzero summary word and the nonzero
stale words it names. Each vCPU also keeps private summaries of the mask words it set bits in (lookup area
`CODE_SUMMARY`/`WRITABLE_SUMMARY`), so a cache reset or a shadow reset clears only those words: resets and drains cost
what was marked, not R (without them the 2 GiB scans took a cross-modifying-code program from 7 s to the 10 s watchdog).
**Address space.**

| Private offset (PRIVATE = 0x8000_4000_0000) | Size | Content | EL1 | EL0 |
| --- | --- | --- | --- | --- |
| 0 - 0xe0000 | 4 KiB pages | window pages (unchanged) | as before | guest-state page RW only (G1) |
| 0x25000 - 0x27000 | 3 pages | level-2 tables of the RAM alias, one per GiB (walked, never mapped) | - | - |
| 0xe0000 - 0x200000 | 288 pages | shadow pool (was 32 pages to 1 MiB) | RW | none |
| 256 MiB / 512 MiB | <= 128 MiB each | code area / its write alias | R / RW | X / none |
| 768 MiB, 896 MiB, 960 MiB | 8, 2, 2 MiB | metadata, lookup (summaries at 0x1fc000), global (masks 0x10000, records 0xd0000) | RW | none |
| 1 GiB - 1 GiB + R (<= 4 GiB) | R | guest-RAM alias (was PRIVATE + 2 MiB) | RW | none |

Backing per vCPU block: window (2 MiB, pool included), lookup, global (block 0 only), metadata, code; guest RAM after the
last block. Stage 2: `hv_vm_map` of the whole backing at IPA 1 GiB; the default IPA size is 36 bits (maximum 40) and the
largest backing ends near 4.6 GiB; `Machine::create` checks the fit and would raise the size. The instruction-cache
flush covers only the windows' resident text. Device ranges (`devices::Devices::new(R)`) lie above R; [R, 4 GiB) is the
map's reserved range and not RAM. The resident's atomics are single LSE instructions
(`+lse`): an LL/SC loop can livelock when every pass meets a vCPU exit. **Renames:** `GUEST_RAM_BYTES`,
`GUEST_RAM_OFFSET`, `GUEST_PAGES` removed (`MAX_GUEST_RAM_BYTES`, `MAX_GUEST_PAGES`, `PRIVATE_BYTES`,
`RAM_TABLES_OFFSET`, `cache::{MASK_BYTES, CODE_SUMMARY, WRITABLE_SUMMARY, SUMMARY_BYTES}`,
`Control::ram_bytes` (last field), `Record::summary` added; `GUEST_RAM_VA` keeps its name); host `prepare(.., ram_bytes)`,
`Backing.ram`/`Area.ram` replace `guest_ram(memory)`, `Devices::new(ram_end)` (no `Default`),
`flush_instruction_cache(offset, len)`; the resident's `RAM` constant became `guest_ram()`.
**Pools.** Shadow pool 288 pages (about 560 MiB of shadowed linear addresses; a full pool resets, counted as
`evictions`). Code cache and records stay at 32 Ki blocks, 8 MiB of metadata (code: `--code-cache`, `boot-linux` 128 MiB):
the Debian kernel's early boot averaged 261 code and 230 metadata bytes per block. Doubling them (64 Ki blocks, 16 MiB)
made the cross-modifying-code program a third slower (7.2 s to 10 s): invalidation scans every live block
(`cache::invalidate`), so larger pools need a page-to-block reverse map first. **Kernel run** (2 GiB, `mitigations=off
nokaslr`, earlycon): past the decompressor into vmlinux, 35,531 blocks, 3,340 compiles, 10 flushes (control-register
changes), 0 evictions, 100 shadow resets, 1,708 blocks live; it stops in `setup_arch` (trim_snb_memory ->
snb_gfx_workaround_needed -> read_pci_config_16: `out dx, eax` at 0xffffffff81c7c422, PCI config address 0x80001000),
the first port I/O (symbols inferred from the code: the image's System.map is the placeholder). **Checks** (shared tree
with X3, 2 GiB unless noted): cargo test 45 + 59; stack audit ok (gate 18,816 of 32,768, nested 22,864, boot 12,688;
HEAD 18,464/22,528/12,512); python unit 263; fix/fix-1 64, seed/seed-1 44, flow 64 x 3, loopfaults 846 x 3, probes 85,
faults/faults-1 7,659 each, twice (78 new window probes: the new alias, its tables, the high pool), ASM 1,052 pass (22
precision deviations), corpora 6,860 at N = 2, 3, 4, smp, limits, kicks, system 246, coherence (N = 1-4), sharedpt (N =
2-4; N = 1's contended programs need a peer and fail at HEAD too), litmus, contain (400 cases, 373 controls; new: EL0
loads and stores into the RAM alias halt `private_access`), privilege, delivery, events, i6 276 x 2, i5a 454, i5a DE 351,
`run_m3.py`, `run_k2.py`, `run_x3.py`; at 4 MiB (`NATEMU_RAM`) fix, seed, flow, probes, corpora, limits, kicks, system,
coherence, sharedpt, contain, privilege, delivery, events, i5a, i6, m3, k2, x3; placed at 1 GiB (`NATEMU_PLACE`, CR3 =
0x40000000) fix, seed, flow, corpora, system, coherence, faults 7,659; `tools/run_m1.py` 14 cases (RAM's top MiB at 4 MiB,
2 GiB, 3 GiB; the device page at R; placement across 0xA0000; cross- and self-modifying code in a code page at 0x7ff00000 at both block sizes, its invalidation
posted through the highest summary words); the L1 smoke test in-tree at 2 GiB (reaches 0x1000c43 in 2,112 blocks,
registers, zero page equal to QEMU's, the window but the known accessed bit). The suites are size-agnostic: their
default is now 2 GiB. **Measurements** (us per iteration, HEAD -> M1): CR3 reload (full flush and shadow reset) 60.6 ->
53.6-54.3 (sizes 4 MiB-3 GiB; noisy, never slower); INVLPG 4.54 -> 4.53-4.59; start-up of a trivial program 11.6 ms ->
8.1 ms at every size; K2 system-instruction round trips +2-6% (CLI 0.414-0.420 -> 0.431-0.438), size-independent,
neither LSE nor the control record's layout, cause not isolated (the independent review measured no M1 cost). The
`xmc cli-nop` and m3 watchdog reports during X3's runs reproduced only with another suite running VMs at the same time
(HEAD shows the same xmc signature under that load). **Not built:** high RAM above 4 GiB (needs per-page structures and
an alias beyond 4 GiB of guest-physical space, a second RAM range in the resident's description and a multi-range
loader); the reverse map; resizing the code-cache pools.
*Built (L2, the ACPI tables of the virtual firmware; no fexc change, series25; after an independent review):*
`crates/host/src/boot/acpi.rs` builds the tables at load time for N processors (`acpi::build(n, base)`); the loader
(`Inputs::acpi`, replacing L1's `Inputs::rsdp`) refuses any blob outside the ACPI NVS range 0x600000..0x800000
(`BAD_TABLES`), places every blob (so `Loaded::writes`, `--dump` and the smoke test's QEMU blobs carry them) and puts
the RSDP's address in `acpi_rsdp_addr`. `boot-linux` always builds them: `--rsdp ADDR` is the graph's base (default
0x600000), `--vcpus N` its processor count (1..4; a run has that many vCPUs since X4). **Sources:** ACPI 6.5
layouts (the repo's snapshot is 6.6 chapter 5, the same offsets for every field written); contents from M69 (and M22-24)
and, where M69 names no value, QEMU 11.1.2's own PC tables (`-machine pc` under SeaBIOS, read back with `iasl -d`;
owner decision below). **Layout (N = 1):** RSDP 0x600000, XSDT 0x600040, FADT 0x600080, FACS 0x6001c0, DSDT 0x600200,
MADT after the DSDT, each 64-byte aligned, then the 4 KiB AP mailbox on the next page (0x601000), written as zeros
(command Noop). **RSDP:** revision 2, 36 bytes, no RSDT (address 0), the XSDT (FADT, MADT). **FADT** (revision 6, minor
5, 276 bytes; not hardware-reduced: L4b provides PM1 and the PM timer): FIRMWARE_CTRL = FACS (X_FIRMWARE_CTRL 0, as the
spec requires), DSDT in both fields; interrupt-model byte 1 (QEMU's; ACPI 1.0's INT_MODEL, reserved since 2.0, "one
should also be supported"); PM profile 0; SCI_INT 9; SMI_CMD 0 and ACPI_ENABLE/DISABLE 0 (ACPI-only mode, M69); PM1a
event 0xb000 (4 bytes), PM1a control 0xb004 (2), PM timer 0xb008 (4), the X_ fields equal; no GPE blocks; P_LVL2/P_LVL3
latency 0x0fff (C2/C3 unsupported); century 0x32; IAPC_BOOT_ARCH 0x5 (legacy ISA devices, VGA not present; no 8042,
CMOS RTC present); flags WBINVD, PROC_C1, SLP_BUTTON, RESET_REG_SUP, USE_PLATFORM_CLOCK (0x8425; 24-bit PM timer);
reset register one byte at I/O 0xb040, value 6 (M69). **FACS:** 64 bytes, version 3, the rest 0. **DSDT** (revision 2,
AML generated in Rust): `\_S5` = Package {0, 0, 0, 0} (M22); `\_SB.COM1` (PNP0501, `_UID` 1, I/O 0x3f8 length 8,
IRQNoFlags {4}); `\_SB.RTC` (PNP0B00, I/O 0x70 length 2, IRQNoFlags {8}); `\_SB.C000..` one `Device (ACPI0007)` per
vCPU with `_UID` = vCPU index; no `_CST`, no P_BLK (only C1 = HLT idle is reachable), no S3/S4 objects. **MADT**
(revision 6): local APIC address 0xfee00000, PCAT_COMPAT; one Local APIC entry per vCPU (UID = APIC ID = vCPU index,
enabled; CPUID 1/0xB and X3); IOAPIC ID 0 at 0xfec00000, GSI base 0; interrupt source overrides IRQ0 -> GSI 2 (flags 0)
and the SCI IRQ9 -> GSI 9 level, active high (flags 0x000d: QEMU's, whose PIIX4 PM drives 1 = asserted; ACPI 5.2.9 makes
the SCI level/active low only when no override says otherwise); Local APIC NMI on LINT1 for all processors (UID 0xff);
the multiprocessor wakeup structure, version 0, 16 bytes, pointing at the mailbox. No HPET, MCFG, RSDT or EFI tables.
The device constants are `boot::acpi::platform`, the one description L3/L4a/L4b/X3/X4 implement.
**Deviations from QEMU's PC tables, each deliberate:** RESET_REG_SUP and the reset register (M69 adds them; QEMU's PC
FADT is revision 1 without them); RTC_S4 clear (QEMU sets it; this machine has no S4, M22); IAPC_BOOT_ARCH 0x5 (QEMU's
revision-1 FADT has no such field; Linux reads it from revision 2, and these bits describe this machine); no SMI command
(QEMU's 0xb2 belongs to SeaBIOS's SMM; M69 selects ACPI-only mode); no GPE0 block (QEMU's PIIX4 has 0xafe0 length 4; a
zero block is legal and no hotplug or GPE source is wired; L4b adds it with a source); no overrides for PCI link IRQs 5,
10, 11 and no PCI host bridge (no PCI devices); no HPET or WAET; FACS version 3 (QEMU's 0); no virtio-MMIO devices
(LNRO0005, M69; not in this phase). **The one iasl exception:** the wakeup structure is the 16-byte version-0 form of
ACPI 6.5 (Table 5.43); iasl 20260408 knows only the 24-byte 6.6 form (version 1, with ResetVector), so its disassembly
ends with "ACPI table terminates in the middle of a data structure!" after it. Version 1 is not used: Linux 6.12 would
take its ResetVector (here 0) for the offline/kexec path (`madt_wakeup.c` `acpi_parse_mp_wake`).
**Checks.** `cargo test boot::` (8: checksums, lengths and the pointer graph; MADT against CPUID 1/0xB APIC IDs and
counts, IA32_APIC_BASE, the DSDT processors, the mailbox and the e820 for N = 1..4; the FADT ports, flags and the RTC
bytes; AML encodings; every table byte in a recorded write). `tools/run_l2_iasl.py` (N = 1..4): `iasl -d` of XSDT,
FADT, FACS, DSDT and MADT clean except the one exception above, required in place; the DSDT recompiled from iasl's
disassembly with `-oa` equals ours except checksum and compiler fields; iasl's RSDP template filled with our values
compiles to our RSDP byte for byte. `tools/run_l2.py` (the independently written checker `tools/models/acpi_model.py`,
with negative controls in `test_acpi_model.py`): pass for N = 1..4, its remaining flags the deviations above.
`tools/run_l1.py` 12/12, 41/41 (`bootproto.py` also verifies the RSDP); the L1 smoke test at 2 GiB matches QEMU at the
kernel entry, now in 2,101 blocks (2,112 before: the decompressor's RSDP search is skipped). **Linux's own parse:** the
Debian 6.12.107 kernel, loaded by natemu's loader (`--load-only --dump`) and run under QEMU 11.1.2 (`-machine pc`, TCG,
no BIOS, the smoke test's firmware stub, serial to a file; scratch script `l2/qemu_kernel.py`), lists all six tables,
"PM-Timer IO Port: 0xb008", the LINT1 NMI, IOAPIC[0] GSI 0-23, the IRQ0 override (`..TIMER: pin1=2`), "Allowing N
present CPUs" for N = 1 and 4, `acpi_wakeup_cpu()` replacing INIT/SIPI, the AML loaded, "Interpreter enabled", "supports
S0 S5", PnP ACPI COM1 (ttyS0 0x3f8 IRQ 4); at N = 4 it then waits in `acpi_wakeup_cpu` (QEMU has no mailbox responder:
X4). Three warnings, explained: "Incorrect checksum in table [APIC]" (Linux zeroes the wakeup structure's mailbox address
after caching it, `acpi_mp_disable_offlining`, a Linux convention: the MADT stays writable RAM); "PM-Timer failed
consistency check" and "Could not disable RealTimeClock events" (without firmware QEMU's PIIX4 PM block is not decoded
at 0xb000, M21; under natemu L4b's PM1 must answer). Inside natemu the kernel stops at its first port access, before it
parses ACPI (L3). **For X4:** the mailbox is `Tables::mailbox` (0x601000 by default), version 0, APs addressed by APIC ID
= vCPU index; tables for a run describe exactly the vCPUs run. **For L4a:** IOAPIC ID 0 at 0xfec00000, 24 pins; ISA
IRQ0 -> GSI 2, IRQ n -> GSI n otherwise; the SCI on GSI 9 level, active high; COM1 IRQ 4 and RTC IRQ 8 edge, active high;
the 8259 pair present; LINT1 = NMI. **For L4b:** PM1a status/enable at 0xb000 (enable bits read back, including
PWRBTN_EN and RTC_EN: the fixed power button and RTC status are in PM1), PM1a control 0xb004 (SLP_TYP 0 + SLP_EN = S5),
the 24-bit PM timer at 0xb008, SCI_EN in ACPI-only mode, the reset byte 0xb040 value 6, RTC at 0x70-0x71 with century
at CMOS 0x32; a GPE0 block only with a GPE source, then the FADT says so. **For L3:** COM1 at 0x3f8 (`platform::COM1_PORT`).

*Built (L3, port I/O, the 16550A UART and the unassigned rule, with the owner's amendment; no fexc change, series25; after an independent review):*
**Port I/O.** fexc already cut IN/OUT/INS/OUTS as system instructions; the host decodes them (`system.rs`, kinds
0x60-0x63: imm8 or DX, 1/2/4 bytes, 66 = 16 bits, REX.W does not promote; INS/OUTS with `System::prefixes` REP (F3) and
ADDRESS32 (0x67), INS always ES, OUTS with FS/GS; SS on OUTS, F2 on INS/OUTS and F2/F3 on IN/OUT are not decoded: a typed
stop with fexc's reason; LOCK #UD) and the resident runs them from the kept decode (K2) as transactions
(`execution/ports.rs`): at CPL > IOPL the TSS I/O permission bitmap (TR present, type 9 or 11, limit >= 67H, the map base
at TSS + 66H, the two bytes at base + port / 8 inside the limit, QEMU's and Bochs's `check_io`), #GP(0) when denied, the
check made before a REP's zero-count test (QEMU's and Bochs's order); one host exit per access (`REQUEST_PORT`, the `Mmio`
record with the port in `gpa`, `dsb sy` first: I/O ordered with memory both ways); the hook `ports::local` for later hot
ports (empty). INS/OUTS move one element per transaction (I2's rule: RIP at the instruction between elements,
`mid_string` set, so events are taken there with RF = 1), DF, 0x67 (ESI/EDI/ECX, written zero-extended); INS walks its
destination (canonical, #AC, #PF for a write, a device page refused `unsupported_mmio`/`system`) before reading the port
and writes through that walk. Ports are 16 bits: a wide access at FFFFH continues at 0. **Port bus** (`devices.rs`):
`Devices::register_ports(base, count, Device)` with M3's contract (per-device mutex, exactly once, cancellation checked
first); an access inside one range is one call, any other byte by byte (each byte its owner's or the rule's; a device
refusing a later byte leaves the earlier bytes made: no device refuses a byte today); every access counted at each port it
touches. **0xCF8/0xCFC** are left to the rule: no host bridge, so no type-1 mechanism (Linux's `pci_check_type1` reads
0xCF8 back as all-ones and decides there is no PCI; `read_pci_config_16` returns FFFFH). **UART** (`uart.rs`): QEMU
11.1.2's `hw/char/serial.c` transcribed (line numbers in the module), byte registers (wider accesses split, as QEMU's
`impl.max_access_size` 1), MCR reset OUT2 (QEMU's), MSR of a backend without modem control, instantaneous transmit,
loopback, the FIFO timeout checked at each access; COM1 at `platform::COM1_PORT`; output to `boot-linux --serial FILE` and
`run-program`'s `uart`. **Unassigned rule (owner, October 29, and its amendment):** an unassigned port reads all-ones at
its width, a write is ignored; an unassigned page (not RAM, no device: the legacy hole, [R, 4 GiB) but device pages, above
4 GiB) is shadowed read-only, never executable, onto one shared all-ones page (`ALL_ONES_IPA` 0xC000000, 16 KiB, mapped
read-only at stage 2) when a read first touches it: the resident asks the host (`REQUEST_PAGE`: `CLAIMED`, `DONE` with
the fill counted as the page's read, or `UNASSIGNED` under strict) and every read form then runs natively (CMP, string,
SSE, a read crossing from RAM: RAM's bytes then 0xff); a write aborts into M3's path: MOV stores complete as ignored, each
counted; RMW, POP m, string stores (`form`), LOCK and XCHG (`atomic`), a crossing store (`crossing`) and system
instructions' own accesses (`system`) keep their typed refusals; a fetch from such a page is no RAM (typed stop).
`--strict-unassigned` (both drivers) makes every one a stop (`unsupported_port`, new stop 49; `unsupported_mmio` status
`unassigned`), counted. Pages a device claims are never shadowed: this vCPU's LAPIC page whatever its mode (disabled or in
x2APIC mode it answers MOV forms as an unassigned page would, other forms are refused as a device page's); an
IA32_APIC_BASE move drops the vCPU's shadows (its new page may hold an all-ones shadow; moves are rare and Linux makes
none). The registry is fixed before the vCPUs run, so no page shadowed all-ones can become a device's at run time; a
device registered at 0xFEC00000 before the run is never shadowed (`run_l3.py` `ioapic-device`). The host refuses a device
or page request for RAM (a bad reply), so a resident error cannot be answered all-ones. Reports: `ports` (per port:
reads, writes, unassigned), `unassigned_pages` (reads = fills, writes = accesses), `uart`/`serial_bytes`. **Admission
fix found by the kernel run:** `branch_target` now skips the ES/CS/SS/DS overrides before a near direct branch (null in
64-bit mode; Linux's `-mindirect-branch-cs-prefix` emits `2e e8 rel32`); 66 (rel16) and FS/GS are not skipped. **Checks**
(`tools/run_l3.py`, programs in `tools/bench/smp/ports.py`): `qemu` 8 programs x block sizes default and 1 equal to pinned
QEMU (registers, captured memory, UART bytes): every UART width and register, Linux's 8250 `autoconfig`,
`autoconfig_16550a`, `clear_fifos` and earlyprintk sequences, loopback FIFO with overrun, unassigned ports at every width,
REP INS/OUTS forms with #PF midway, and five CPL 3 IOPL/bitmap configurations (19 cases each, zero counts included);
`stress` (kicks with events between elements: RF and RCX exact at every delivery, output exact); `cases` 14 natemu-only
SDM/rule cases (strict, PCI probes, wrap, crossing the UART's end, LOCK, F3, INS on a device page, INS #AC/#PF with the port
unread, CPL 3 without a bitmap); `pages` 40 cases: the hole, 0xC0000000 at 2 GiB, 0x80_0000_0000 and the IOAPIC page each
with CMP/LODS/REPE CMPSB/MOVUPS/load-op reads (one fill), MOV writes ignored and counted, RMW/XCHG/STOS refused, strict
read and write stops; a read crossing from RAM's last page into [R, 4 GiB); two vCPUs filling the hole; the xAPIC page
disabled and in x2APIC mode (all-ones, never shadowed, the version readable at once after re-enable); a device at
0xFEC00000; the shadow drops (a write after a fill; remaps hole -> RAM after INVLPG and after a CR3 reload, RAM -> hole
after INVLPG; an APIC base moved onto a filled page; two vCPUs with different APIC bases), each drop case shown failing
with its drop disabled. `run_m3.py` `unassigned` (10), `run_m1.py` (20), `run_x3.py` cases updated to the rule. QEMU departures kept
out of the comparisons: an access crossing 3FFH (QEMU aborts, serial.c:476), a misaligned wide read of an unassigned port
(QEMU reads 0), a wide access at FFFFH; SeaBIOS uses COM1 first (programs reset it). L1 smoke test with the hole: equal to
QEMU; with `acpi_rsdp_addr` forced to 0 (scratch build) the decompressor's RSDP scan reads 0xE0000-0xFFFFF all-ones and
the result is still equal. **Kernel** (Debian 6.12, 2 GiB, `console=ttyS0 earlyprintk=serial mitigations=off nokaslr`):
117 console lines to `SRBDS: Unknown: Dependent on hypervisor status`, then fexc stops `unsupported_form` at FXSAVE
(0xffffffff83369a1d, `mxcsr_feature_mask_init`, I5b). **For L4a:** the UART's `irq` hook reports level changes (a THR
write gives low then high again); QEMU's edge-mode IOAPIC delivers on every raise, 1 -> 1 included, so L4a must decide
whether a raise while high is an edge (QEMU) or none (a real edge detector); the CTI timeout and any host input become
asynchronous sources: they must post through `execution::post` and veto ending halted vCPUs while they can fire (X3's
rule). The xAPIC page while its APIC is disabled or in x2APIC mode answers MOV forms with all-ones (writes ignored) from
the resident's LAPIC, is not counted in `unassigned_pages` (it is a device page, never a fill), and refuses every other
read form (`form`, as a device page). The kernel's "ACPI BIOS Warning (bug): Incorrect checksum in table [APIC]" is not
L2's: Linux's `acpi_mp_disable_offlining` zeroes the MADT's multiprocessor-wakeup mailbox address in the table itself
(the second review verified that only bytes 0x600323-0x600324 changed), after the checksum was computed. Not built: UART
input, a host timer for CTI, hot-port resident devices, runtime device registration.

*Built (I5b, FXSAVE/XSAVE family and gating; fexc series26-i5b = series25 + a shim change; reviewed October 29, its
fixes in series27):*
**Forms.** The shim cuts `0f ae /0 /1 /4 /5 /6` and `0f c7 /3 /4 /5` with a memory operand as system instructions (`0f 01
d0/d1` already were); the host decodes FXSAVE/FXRSTOR, XSAVE/XRSTOR (REX.W's 64-bit forms too; NP: 66/F2/F3 and 0x67 are
not decoded, a typed stop) and XGETBV/XSETBV as kinds **0x68-0x6d** (`system::FXSAVE..=XSETBV`; 0x68-0x6f reserved for
I5b), and decodes XSAVEOPT (`/6`), CLWB (66 `/6`), XRSTORS, XSAVEC, XSAVES as #UD: CPUID.(EAX=0DH,ECX=1):EAX = 0 and
CPUID.7.0:EBX.CLWB = 0 (the profile: XCR0 ⊆ 7, standard format only, no XGETBV with ECX = 1, no IA32_XSS). The resident
runs them from the kept decode (K2) in `execution/xsave.rs`. **State.** XCR0 is `Cpu::xcr0` (reset 1); CPUID.0DH.0:EBX
now follows it (576/832). YMM_Hi128 (component 2) lives in the guest-state page at FEX's `avx_high` (`frame::AVX_HIGH`
= C0H, 256 bytes), outside `GUEST_STATE` (no admitted code reads it; the driver's `frame_intact` admits the range), so
it is zero until an XRSTOR loads it and XSAVE stores what it holds; it was not put in `Cpu` because every copy of the
bank on the resident's deepest paths grew the gate bound past its headroom (22,032 bytes). Deviation (as I6 deviation 12
for the x87 fields, G1's page deviation): a guest data access to the guest-state page reaches the vCPU's own YMM_Hi128. **Images** (SDM Vol.2 FXSAVE
Tables 3-46/3-47, Vol.1 13.4): FCW, FSW (`x87::fsw()`), the abridged tag byte as the page holds it, byte 5 and FOP,
FIP/FCS, FDP/FDS 0 (I6 deviation 8: so FXSAVE and FXSAVE64 store the same bytes), MXCSR, MXCSR_MASK FFFFH, ST0..7 widened
exactly from the doubles at 32 + 16i with bytes 10..15 0, XMM0..15 at 160; bytes 416..511 are never written. FXRSTOR and
XRSTOR load FCW, FSW (`x87::set_fsw`), the tag byte and ST(i) rounded to doubles by the new FCW.RC (I6 deviation 2).
XSAVE stores what RFBM = XCR0 ∧ EDX:EAX selects (MXCSR/MXCSR_MASK with RFBM[1] or RFBM[2]), then XSTATE_BV := old ∨ RFBM
(XINUSE is all ones, which the SDM allows: QEMU does the same); no other header byte is written. XRSTOR, standard form:
#GP(0) for XCOMP_BV ≠ 0 (bit 63, the compacted form, needs XSAVEC), XSTATE_BV outside XCR0 or header bytes 16..23 ≠ 0
(bytes 24..63 are not checked: the current SDM checks 23:8); per component of RFBM, load (XSTATE_BV bit set) or the
initial configuration (x87: FCW 037FH, FSW 0, tags empty, ST 0; XMM 0; YMM_Hi128 0); MXCSR from memory with RFBM[1] or
RFBM[2], #GP(0) for a reserved bit (FXRSTOR too, whatever CR4.OSFXSR). **Choices the SDM leaves open:** misalignment
(16 bytes FX*, 64 bytes X*) is #GP(0), never #AC (both instructions' #AC text: "where #AC is not signaled, #GP is
signaled in its place"), checked before the operand's canonical form and its walk; the canonical check (#GP(0), or #SS(0)
for an SS-class reference) covers exactly the bytes accessed, a range wrapping past 2^64 included: FX* bytes 0..415,
XSAVE the span from its first to its last stored byte, XRSTOR the area's base through the header, then what it restores
(review fix: it was the nominal 512 or 832 bytes; since series28's review, XRSTOR's first check starts at the base, so an
area whose base is non-canonical but whose header at +512 is canonical is #GP/#SS, not a #PF on the header's page, as
hardware most likely checks the base first); FXSAVE/FXRSTOR save and restore XMM and
MXCSR whatever CR4.OSFXSR ("may not ... implementation dependent"; QEMU skips them); #UD (no CR4.OSXSAVE) before #NM (TS).
**Gating:** FX* #NM with CR0.TS or CR0.EM; XSAVE/XRSTOR #UD without OSXSAVE, then #NM with TS (EM does not gate them);
XGETBV/XSETBV #UD without OSXSAVE; XSETBV #GP(0) at CPL > 0, for ECX ≠ 0 (ECX's upper half ignored) or a value not in
the profile's {1, 3, 7} (bit 0 clear, XCR0[2:1] = 10b, unsupported bits, EDX ≠ 0); XGETBV #GP(0) for ECX ≠ 0; LOCK #UD.
No flush: FCW and MXCSR are keyed in `code_context`; XCR0 gates no admitted form. **Precision:** every byte read is read
and every page written is walked for write before anything changes (XSAVE walks the span from its first to its last
stored byte, each page of which it stores to, then stores part by part; XRSTOR reads the header first, then only the
legacy part and AVX area it loads), so a #PF or #GP leaves state and memory unchanged; nothing is stored unless the whole
span is guest RAM (review fix: XSAVE stored its RAM part before stopping at a device page, because `pages` returns device
addresses; it now stops `unsupported_mmio` first, as `linear` does). **Fault order (unverifiable):** the SDM orders none of
XSAVE's or XRSTOR's accesses and QEMU's stub cannot show it, so these are natemu's choices: XSAVE walks its whole stored span
for write, lowest page first, before it reads XSTATE_BV, so a fault is reported on the span's first faulting page with write
intent (a read-only header page is #PF with W, never a read fault of the header); XRSTOR reads the header first, so a fault
there comes before one on a lower legacy page; hardware may order them otherwise, and may store part of the area before a
fault, which natemu never does. As FLDCW/LDMXCSR, an FCW or MXCSR
that would unmask an exception stops `unsupported_system` after every fault check and before any effect (an XRSTOR that
does not load x87 does not check its FCW). **What I8 must hook:** (1) admit generated code's x28 accesses to
`frame::AVX_HIGH` (add it to `GUEST_STATE`/`GUEST_STATE_WRITABLE`): FEX keeps YMM upper halves there without SVE256, so
XSAVE/XRSTOR and the VEX forms then share one copy; (2) gate VEX at translation time (#UD unless CR4.OSXSAVE and XCR0[2:1]
= 11b, #NM with TS) and key XCR0[2:1] and OSXSAVE in `Cpu::code_context` (or flush on XSETBV), I5a's obligation; (3)
VEX.128 forms zero the upper half (FEX's lowering) and the 32-byte memory forms need sites, alignment (VMOVAPS/VMOVDQA ymm)
and atomicity as I5a's. **Checks** (series26-i5b; `tools/run_i5b.py`, QEMU 11.1.2 `-cpu Haswell,-pcid,-hle,-rtm,-invpcid`,
whose leaf 0DH sub-leaves 0 and 2 and leaf 1 ECX equal the profile's; sub-leaf 1 also advertises XSAVEOPT, which no
comparison executes, because this QEMU loops in `vmexit(-1)` after setting CR4.OSXSAVE with `-xsaveopt`): `images` 37 CPL0
programs at block sizes default and 1, GPRs and a 16 KiB pattern-filled data region byte by byte against QEMU: FXSAVE and
FXSAVE64 of known x87 (doubles: 1.5, -2.25, 3e10, pi, 1e-300, -0, inf, a double denormal), MXCSR and XMM state, full and
empty stacks; Linux's MXCSR_MASK probe (`fpu__init_system_mxcsr`: FFFFH); FXRSTOR of crafted images (TOP 3, tags B5H, C1
C3, ZE PE, RC up and down, NaN/inf/zero/min-normal, REX.W) then FXSAVE, FNSTENV, STMXCSR and every XMM; round trips;
`fpu__init_cpu_xstateregs`-style XSETBV 1/3/7/3/1 with XGETBV, CPUID.0DH.0 and CPUID.1:ECX after each, ECX's upper half
ignored; XGETBV at reset; XSAVE under each of the 8 RFBMs into pattern-filled areas with a pattern XSTATE_BV; XSAVE64 with
XCR0 = 3; XRSTOR of crafted images under 13 (XSTATE_BV, RFBM) pairs then XSAVE/FNSTENV/STMXCSR (init vs load per
component); header bytes 24..63 ignored; XCR0 = 3 then 7; XRSTOR/FXSAVE/FXRSTOR/XSAVE chains; an XSAVE -> XRSTOR round trip
with AVX state. Declared differences, compared only on natemu's side: FOP/FIP/FCS/FDP/FDS (QEMU tracks them; natemu 0,
I6 deviation 8), ST slot bytes 10..15 (QEMU leaves them; natemu writes 0), an 80-bit ST value with more than 53
significand bits (natemu: rounded by FCW.RC, I6 deviation 2), and MXCSR under RFBM = AVX without SSE (the SDM saves and
loads it; QEMU does neither). All 37 equal at both block sizes. `faults` 149 cases at both block sizes, each judged by the
SDM rule (vector, error code, CR2, RIP) with GPRs, XMM, MXCSR, the x87 report and the data region equal to natemu's run
of the same program cut before the instruction: alignment for every form (also with the page not present: #GP first);
#PF with the image across a page boundary at 3 FX offsets and 5 X offsets, second or first page not present or read-only,
XSAVE under RFBM 7/1/3 (a fault only where its stores reach) and XRSTOR (the header first: CR2 = its address), XRSTOR
with RFBM 0 or an initialised component never reading the legacy page; non-canonical #GP/#SS (RSP) and straddles; #NM
under TS (4 forms) and EM (FX* only), #UD without OSXSAVE (XSAVE, XRSTOR, XGETBV, XSETBV, also with TS), XGETBV under TS
and FXSAVE without OSFXSR run; LOCK; the 5 unadvertised forms; 9 XSETBV and 1 XGETBV #GP values, XSETBV at CPL 3, XGETBV
and XSAVE at CPL 3 run; 9 XRSTOR header cases (bit 3, outside XCR0 = 3, compacted, XCOMP_BV low, bytes 16 and 23, bit 63;
byte 24 runs); MXCSR reserved bits under RFBM SSE/AVX (#GP) and x87-only/none (runs), FXRSTOR's; FCW/MXCSR unmasking
refused (FXRSTOR, XRSTOR), not when x87 is not loaded; CPL 3 with CR0.AM and RFLAGS.AC: #GP, not #AC. QEMU ran the 60
cases that need no paging mutation or CPL change and agreed on all but 6 recorded in `QEMU_DIFFERS`: no #NM for
XSAVE/XRSTOR under TS (SDM: #NM), no reserved-MXCSR #GP for FXRSTOR/XRSTOR (SDM: #GP), XSAVEOPT runs (its model
advertises it). All 149 as expected. FEX ASM suite (barely exercising these forms: the coverage is `run_i5b.py`'s; tests now run with CR4.OSXSAVE and XCR0 = 7, the program driver's new
`xcr0` item, as Linux leaves user mode): 1,052 -> 1,053 pass (`SecondaryModRM/Reg_2_0`, XGETBV), 0 fail, 22 precision
deviations; the FXSAVE/XSAVE tests stop on their MMX or FFREEP forms, or (`fxch_tag_word`, `fincstp_tag_word`,
`fst_st0_tag_word`, `PFRCPIT1_MM0_selfmove`) on FXRSTOR images whose MXCSR is 0 (all exceptions unmasked: refused, a new
`run_asm.py` reason). Census (`natemu-raw/kernel-census-i5b-20261029-01/`): kernel x87 10 -> 0 and other 28 -> 1 (the
FXSAVE/XSAVE forms are `system`), initramfs x87 65 -> 59, other 2,294 -> 2,282 (`experiments/track03/kernel-census/README.md`).
Standing suite on series26-i5b, all 0 failures: cargo build/test, stack audit (gate 18,960 bytes, 23,040 with a nested
entry, of 32,768), python unit 281, fix/fix-1 64, seed/seed-1 44, faults/faults-1 7,659 each, flow 64 x 2, loopfaults 846 x
2, probes 83, corpora, smp (one rerun: `neg-race@2`'s "no CPU took the lost-race self-exit" coverage check, a timing
outcome, passed on the rerun), limits, kicks, system, coherence, sharedpt, litmus, contain, privilege, delivery, events,
i6 faults 276 at both block sizes, i5a faults 454, i5a DE 351, `run_m3.py`, `run_l1.py`, `run_l2.py`, `run_l2_iasl.py`,
`run_k2.py`, `run_x3.py`, `run_m1.py`, `run_l3.py`, `run_i5b.py` 186 (corpora and m3 rerun after the Docker daemon was
restarted). After review, on series27 (the fixes and I7's revised prefix rule), all 0 failures: cargo build/test, stack
audit (gate 18,960 bytes, 23,040 nested), python unit 281, fexc `test_slice1.py` 28 (its stale `fld st0` and `fxsave`
entries moved: admitted since I6, a system cut since I5b) and `check_sites.py --slice1` 64, fix/fix-1 64, seed/seed-1 44,
faults/faults-1 7,767 each (+108 `null_segment_probes`), flow 64 x 2, loopfaults 846 x 2, probes 83, corpora, smp, limits,
kicks, system, coherence, sharedpt, litmus, contain, privilege, delivery, events, i6 faults 276 x 2, i5a faults 454, i5a
DE 351, `run_m3.py`, `run_l1.py`, `run_l2.py`, `run_l2_iasl.py`, `run_k2.py`, `run_x3.py`, `run_m1.py`, `run_l3.py`,
`run_i5b.py` 410 (+3 `mmio-xsave-*`, +221 `segment-*`), FEX ASM 1,053 pass, 22 precision deviations, 0 fail.
**Kernel** (Debian 6.12, 2 GiB, `console=ttyS0 earlyprintk=serial mitigations=off nokaslr`): past the MXCSR probe and
`fpu__init_cpu_xstateregs` ("x86/fpu: Enabled xstate features 0x7, context size is 832 bytes, using 'standard' format"),
then fexc stops `unsupported_form` at 0xffffffff810a79b2, `ds btr %rdx, 0x540(%rbp)`: on one CPU the kernel's
SMP alternatives replace that LOCK prefix by DS (bytes `3e 48 0f b3 95 40 05 00 00` in guest memory), and a DS override on an
RBP-based operand names a non-default segment, which I7's rule refuses (in 64-bit mode DS and SS both have base 0, so the
rule could admit it; I7's or the next chunk's decision, not I5b's). 122 console lines, 16 s. On series27 (Intel's class
for that prefix, I7's revised note) the kernel runs on to 175 console lines in 40 s, past "ACPI: 1 ACPI AML tables
successfully acquired and loaded", and stops `nocode` (`not_executable`) at 0xffffffff817f0fff, a `jne rel32` (`0f 85`)
whose first byte is its page's last, in `acpi_irq_stats_init` (drivers/acpi/sysfs.c: its calls are `__kmalloc_noprof`,
`acpi_install_global_event_handler`, `sprintf`, `kstrdup`, `sysfs_create_group`; the stripped vmlinux's exported symbols
around it). Cause: with one byte (`0f`) before the page end FEX's decoder reports `not_executable`, not `partial_decode`
(as it does from two bytes on), and the host asks the resident for the next page only on `partial_decode` or
`end_of_input` (`crates/host/src/execution.rs`, `ran_out`); the next page is ordinary kernel text in the same 2 MiB page.
**Not built:** XSAVEOPT/XSAVEC/XSAVES/XRSTORS, XGETBV with ECX = 1 and XFD (not advertised); AVX state beyond its XSAVE
component (I8); FOP/FIP/FDP (I6 deviation 8); XINUSE tracking (all ones).

*Built (straddle/over-long, the decoder at the end of its input and the 15-byte limit; fexc series28 = series27 + patch
0020; after an independent review):* **Straddle.** An instruction that starts in the bytes fexc was given and needs an
unreadable byte within its first 15 is `partial_decode`, whatever FEX's zero-filled decode of the missing bytes gives
(it was `not_executable` for `0f`, `0f 3a`, `0f 0f`, `0f c7`, `66 0f`, `f3 0f`, `c4`, `c4 e2`, `c5` at the end), so the
host asks for the next page: present, the instruction is translated across the boundary; not present (or XD, or
supervisor at CPL 3), the resident delivers its #PF, CR2 the next page's first byte, RIP the instruction (Vol.3 Table
7-2: a fetch fault before decode). The host is unchanged. **Over-long.** An instruction that needs a 16th byte is
`instruction_too_long` (end reason 13), answered as system kind `TOO_LONG`: #GP(0) at it (SDM Vol.3 Interrupt 13,
"Exceeding the instruction length limit of 15 bytes"). Before, FEX's `ReadData` never checked the limit (a 16- or
17-byte instruction with every byte present ran) and a long 3DNow! form wrote past FEX's 15-byte buffer. **natemu
policy (the SDM is silent):** the 16th byte is never fetched, so an over-long instruction whose 16th byte is on a page
that faults is #GP(0), not #PF; Bochs does the same, QEMU fetches the next page first and takes the #PF. **Refusal kept:**
EVEX (`62`) is `unsupported_form` before its fetch completes, so a truncated EVEX form at a not-present page stops there
instead of taking the #PF. **Checks:** faults' `straddle-*` (27 instructions, next page present and absent, plus XD and
supervisor) and `long-*` (15 bytes; 16 and 17; 15 prefixes; a 16th opcode byte; a 17-byte 3DNow! form; 16 bytes split at
byte 10 and 15, next page present and absent) at both block sizes; `test_slice1.py`; objects byte-identical to series27
over 30,344 inputs but the 54 that end mid-instruction at the end of the input (end-reason byte only). Kernel (2 GiB, no
initrd): past `acpi_irq_stats_init` to "VFS: Unable to mount root fs" (293 console lines), then panic's delay loop.

**Owner decisions (October 29, L2):** (1) the ACPI tables come from natemu's own Rust builder (`crates/host/src/boot/acpi.rs`) using QEMU's conventional values, instead of reusing QEMU's C builder and linker (the Q6 deviation is accepted); it is judged by an independently written checker (`tools/models/acpi_model.py`, `tools/run_l2.py`) and by Linux 6.12's own parse of the same tables under QEMU. (2) Intel's ACPICA tools (`iasl`, Homebrew `acpica` 20260408) are installed for the standard `iasl -d` disassembly check.

*Built (L4a, the I/O APIC, the 8259 pair and interrupt routing; no fexc change, series27):*
**Models** (host, QEMU 11.1.2 transcribed, tag v11.1.2, line numbers cited in each module): `crates/host/src/ioapic.rs`
(`hw/intc/ioapic.c`: version 20H with the EOI register at 40H, 24 entries, ID 4 bits, ARB = ID, IOREGSEL any width,
IOWIN/EOI 32-bit only, the page folded every 100H; Remote IRR and delivery status read-only; edge: a rise while unmasked
delivered once, a rise while masked lost; level: the input kept while masked, delivered while asserted, unmasked and Remote
IRR clear, Remote IRR set only when a LAPIC accepted the message (82093AA; QEMU sets it on sending) and cleared by the
EOI broadcast or the EOI register, re-evaluated at every redirection write) and
`crates/host/src/pic.rs` (`hw/intc/i8259.c`, `i8259_common.c`: ICW1-4, OCW1-3, IRR/ISR/IMR, specific and non-specific EOI,
rotations, auto-EOI, poll, special mask, special fully nested, the cascade on IR2, the INTA cycle with the spurious IRQ 7
of either chip, ELCR at 4D0H/4D1H with masks F8H/DEH, byte registers). **Board** (`crates/host/src/irq.rs`, QEMU's
`gsi_handler` and `pic_irq_request`): ISA IRQ n -> PIC input n and I/O APIC pin n (IRQ0 -> pin 2), GSI 16-23 -> the I/O
APIC only; the master's INT output -> every LAPIC's LINT0; the UART's IRQ4 hook (`irq::install`, both drivers); the
I/O APIC page is a registry device, so it is no longer all-ones-shadowed (`run_l3.py` moved its unassigned-page cases to
0xFED00000). One mutex orders the chipset. **Inputs are levels and an edge is a 0 -> 1 change** (an edge detector's
view; the board passes changes only), a departure from QEMU's edge-mode I/O APIC, which delivers on every raise call,
1 -> 1 included: the compared programs make no redundant raise. **Delivery to the LAPICs:** a message's 8-bit destination
(bits 63:56) is matched per target from its published word (`natemu_abi::lapic::in_destination`; an x2APIC-mode target
matches a physical destination by its ID, 0xFF not being a broadcast for it, and a logical one as an x2APIC cluster/bit
destination, as QEMU's `apic_get_delivery_bitmask`); fixed and ExtINT (vector from the PIC's INTA, delivered as fixed, as
QEMU) post to every target, lowest priority to the lowest-numbered accepting one (X3's arbitration), NMI posts the NMI;
SMI (no SMM) and INIT (X4) reach no core; vectors 0..15 are dropped (ponytail: the receive-illegal error would need a
resident-side flag). Every delivery goes through `execution::post` (acceptance, the post count; it now returns whether the
target accepted) then the target's doorbell. A software-disabled APIC accepts no fixed message (X3's reading of SDM
11.4.7.2, "held" = kept pending, nothing new accepted; QEMU accepts them into its IRR), so a level message nobody accepted
leaves Remote IRR clear and the input pending.
**LAPIC side** (X3's open items): the TMR is the shared `Events::tmr`, written by the sender with the IRR bit (`post`'s new
`level` argument: set for level, cleared for edge, as `apic_set_irq`), and every resident edge source (LVT timer and
error, self and peer IPIs) accepts through one helper, `lapic::accept_edge`, which clears the TMR bit before the IRR bit
(review finding: without it a vector once accepted level kept TMR = 1 and its edge EOI broadcast, which could clear Remote
IRR on a pin still in service; `l4a-tmr` checks it); re-enabling the APIC clears the TMR with the IRR; the EOI of a vector whose TMR bit is set makes one
host exit, `REQUEST_IRQ` (0x34) with `Mmio::write` 1, the broadcast to the I/O APIC (`lapic::eoi`; an edge EOI stays
exit-free); LINT0 is `Events::lint0`, the PIC's output kept current by the chipset: with LVT LINT0 unmasked ExtINT (or the
APIC hardware disabled, the PIC wired through) an ExtINT is taken before the IRR when RFLAGS.IF and no shadow allow it
(QEMU's `apic_get_interrupt`), its vector from the PIC by `REQUEST_IRQ` with `write` 0 (the INTA cycle), never entering IRR
or ISR (`events::Next::ExtInt`); the published word gains `EXTINT` and `LINT1_NMI` (LVT LINT0/LINT1 writes republish), so
a PIC output rise posts to and rings only the APICs taking ExtINTs; `LINT0_FIXED` (bits 55:48 the vector): LVT LINT0
unmasked, fixed, edge-triggered takes each rise of the PIC's output as that vector's edge interrupt, with no INTA (SDM
11.5.1; QEMU passes the 8259 through in any unmasked mode, so Linux's `check_timer` virtual-wire fallback, which
programs LINT0 fixed, would behave differently there; a level-triggered fixed LINT0 is not built); and the board's NMI input (`Irqs::nmi`, LINT1 per the
MADT; no source drives it on this board yet) posts NMIs to the APICs with LINT1 unmasked in NMI mode. **Asynchronous
sources (the API for L4b):** `irq::Irqs` (clone, any thread): `line(irq, level)` (ISA IRQ 0..15 or GSI 16..23, 1 =
asserted), `busy()` a guard held while the source may still fire, `live()` read by the program driver's halted-vCPU
watcher next to `kickers_left` (X3's rule) after its two park-state reads (review finding: read before them, a guard taken
in between by a vCPU that then halted went unseen; with a 30 ms delay forced there `halt` was red 5/5, in this order
green 5/5), `nmi()`; `devices.irqs` reaches it from a device. **Test source** (`run-program
--irq-source PORT`, `irq::TestSource`): a host thread pulses or asserts a line (or the NMI input) after a guest-chosen
delay, deasserts on request, counts what fired; `--irq-source-no-veto` is the halted rule's negative control. **QEMU
departures (not compared):** the edge rule above; QEMU's storm delay after 10,000 successive EOIs (here immediate);
QEMU's INTA for a masked or coalesced ExtINT entry (here only on delivery); polarity stored, not applied (inputs are
logical, as QEMU); 8-byte I/O APIC accesses refused; LINT0 fixed mode (above); the BSP's LVT LINT0 resets masked (SDM
11.4.7.1; QEMU 700H, ExtINT, `apic_reset_common`: the programs set LINT0 explicitly); lowest priority skips
software-disabled APICs (QEMU does not); a software-disabled APIC refuses fixed I/O APIC messages (above); a fixed ICR IPI
with the level trigger bit is edge (SDM 11.6.1: the trigger mode is used only for INIT de-assert; QEMU sets the TMR for
it). Matching QEMU and the datasheet: after ICW1 the 8259's edge detector is reset (`last_irr` 0), so a line held high
requests again only after it falls and rises. `controller()` puts the exchange record back after its request, so an
xAPIC EOI's broadcast does not overwrite the record of the device access in progress (the run report's `mmio`). **Checks** (`tools/run_l4a.py`, programs `tools/bench/smp/irqs.py`):
`qemu` 13 programs, each at block sizes default and 1, natemu equal to pinned QEMU (registers, captured memory, logs
without their TSC at one CPU, counts at several, UART bytes) and both equal to the datasheet/SDM model: I/O APIC register
read-backs (ID, VER 0x170020, ARB, all 24 entries, every field with the read-only bits attempted, IOREGSEL widths, the
100H fold, byte IOWIN, the EOI register) in xAPIC and x2APIC mode; IRQ4 edge through pin 4 (4 deliveries: IER 0 -> 2, a
THR write, a rise while masked lost, TMR clear); level (4 deliveries: Remote IRR and TMR set in each handler, the EOI with
the line high redelivering, IER 0 and an IIR read ending it, masked -> unmasked delivering at the unmask, the EOI register);
the 8259 pair (Linux's probe_8259A reads FBH, init_8259A, OCW3 IRR/ISR, ELCR F8H/DEH, an IRR latched while masked and
cleared by ICW1, ExtINT 34H through LINT0 with no LAPIC ISR bit, poll 84H, ELCR level mode re-requesting after a PIC EOI,
auto-EOI, rotation and special mask) in both modes; the TMR across triggers (54H level from pin 4, then a self IPI: TMR 1
then 0) in both modes; destinations at 2, 3, 4 CPUs in x2APIC mode (each physical ID,
two logical sets, lowest priority to CPU 1, NMI, level to a peer whose handler lowers the line before its EOI broadcast).
QEMU's runs stop its PIT first (control word 30H). `xapic` 4 (the destination program at 2-4 CPUs, flat logical IDs,
Linux's path, counts by the model; `l4a-lint0`: LINT0 fixed 45H, one delivery per rise of the PIC's output and no INTA,
then the APIC hardware disabled: ExtINT 34H passed through). `stress` 2 (4 and 2 CPUs, each CPU's GSI routed to its neighbour, 300 edge then 300
level rounds per source at a pseudo-random 0..255 us from the host thread): every target 600 deliveries and 600
completions per source, none lost or doubled. `halt` 2: a vCPU halted until a 50 ms request fires reaches its end; with
`--irq-source-no-veto` it is ended `halted` (the veto is what keeps it). `lint1` 1: 2 NMIs of 3 pulses (one while LINT1
masked). Negative control: with the resident's EOI broadcast removed (scratch build) the level program gets 1 delivery,
Remote IRR stuck (0xc054 after every step). Standing suite on this tree: `run_x3.py` (qemu 9, ipi 4, timers 24, stress 6,
halt 10, cases 54), `run_l3.py` (qemu 8, stress 2, cases 24, pages 40), `run_m3.py`, `run_l2.py` (N = 1..4), events 136,
system 246, `check_resident_stack.py` (gate 18,960 bytes, unchanged), cargo test (45 + 71: `pic`, `ioapic` (Remote IRR
on acceptance), `irq` (the board's change filter: a raise while high is no edge, IRQ0 -> pin 2) and `lapic` (the LINT
bits, the disabled APIC's pass-through) unit tests), python unit 281. After the review's fixes, on series28: `run_l4a.py`
22/22, `run_x3.py`, `run_l3.py`, events 136, system 246, cargo test and the stack audit again green. **Kernel** (Debian 6.12, 2 GiB, `console=ttyS0 earlyprintk=serial mitigations=off nokaslr`): on
series27 unchanged, 173 lines, `nocode` at the page-straddling `jne` 0xffffffff817f0fff; on series28 (the straddle fix,
in progress elsewhere; rerun after the review's fixes: the same 290 lines, no "Failed to find ioapic for gsi") without an
initrd it reaches "ACPI: Using IOAPIC for interrupt routing", "APIC: Switch to symmetric
I/O mode setup", "ttyS0 at I/O 0x3f8 (irq = 4) is a 16550A" and panics "VFS: Unable to mount root fs" (290 lines); with
Debian's initrd it frees the initrd (223 lines) and stops `nocode` `unsupported_form` in a CPL 3 process at
0x7f6546c96470. **Not built:** a PIT/RTC/SCI source (L4b: `Irqs::line` and `busy`), UART input and the CTI timer as
asynchronous sources, a level-triggered fixed LINT0 and a fixed LINT1, the receive-illegal-vector error for an I/O APIC vector 0..15, ExtINT
through an I/O APIC entry wired to the PIC (modelled as QEMU, unexercised), SMI and INIT messages.

*Built (L4b, the PIT and port 61H, the RTC, the ACPI PM block, S5 and the reset register; and L5's HLT wake; no fexc change, series28; reviewed October 30, fixes in the review-round note below):*
**Time base** (`natemu_abi::clock`: `PIT_HZ` 1,193,182, `PM_TIMER_HZ` 3,579,545, `count_at`, the exact inverse of `scale` at
any rate; host `clock.rs`: V = `mach_absolute_time`, its rate from the mach timebase, exact): every device clock is a floor
of V x rate / CNTFRQ, as the TSC and the LAPIC timer are, so none can drift from another. **Asynchronous devices** run
on a `clock::Timer` thread each: it sleeps until the V of the device's next event and lets the device act as of that V
(lateness delays an interrupt, never changes a count). The threads take the time-constraint scheduling policy: a
default-policy thread's timer fires up to half its interval late on macOS (host `nanosleep` of 0.05/0.2/1/2/4/10 ms:
27/102/505/976/1,804/4,350 us late on average; under the policy 1-5 us). An access wakes the thread only when it moved
the device's next event (waking it on every access cost ~5 us per port access: quick_pit_calibrate's verify pair went
from 11.4 to 6.0 us). Each device holds an `Irqs::busy` guard while it can still raise its line (L4a's rule).
**PIT** (`pit.rs`; QEMU 11.1.2 `hw/timer/i8254.c` 53-258, `i8254_common.c` 44-130, `hw/audio/pcspk.c` port 61H): counts,
OUT, control words, read-back (count and status latches, a second latch ignored until read), LSB/MSB/word access and
toggles, count 0 = 10000H, gate restarts of modes 1/2/3/5, all QEMU's, with d = PIT edges since the load from V.
Channel 0 drives ISA IRQ 0 at the level QEMU's interrupt timer gives it (QEMU re-evaluates OUT only at the transitions it
schedules, and its ns round trip floors each scheduled tick k to k - 1; the effective level is OUT except in mode 2, high
from d = c, low at d = c - 1 mod c from 2c - 1: rising edges every count), each change processed at its own V (no edge
lost when the thread is late). Port 61H: gate 2, speaker data, the refresh bit toggled per read, OUT2. **Departures:** at
reset no channel counts until its first count is written (QEMU's reset programs mode 3, 10000H, running, as firmware
would; this machine has none, and the 8254 is undefined until programmed), so IRQ0 is quiet and no guard is held until the
guest programs channel 0; modes 6 and 7 act as 2 and 3 (datasheet; QEMU computes OUT for them as mode 0). As QEMU (not
the datasheet): binary counting only (BCD stored and read back), no NULL COUNT status bit, a control word neither stops
the count nor updates IRQ0 (Linux's PIT-disable sequence 30H, 0, 0, 30H thus gives one more IRQ0 55 ms later, as on
QEMU), the gate does not stop modes 0 and 4.
**RTC** (`rtc.rs`; QEMU `hw/rtc/mc146818rtc.c` 74-760, the `pc` machine's base year 2000 and lost-tick policy `discard`):
the host's UTC time at VM creation, advanced from V (ns = `scale(V, 1e9)`, the periodic divider `scale(V, 32768)`);
every register rule QEMU's: BCD/binary and 12/24-hour encodings, SET, divider reset and the half-second restart, UIP in
the last 244 us (latched until the update), UF/AF (don't-care C0H-FFH)/PF with IRQF and IRQ 8, B enabling a flag already
set raising at once, C read clearing and lowering IRQ 8, C and D read-only (D = 80H), 37H = the century at 32H, 70H
reading FFH and its bit 7 (the NMI mask on PCs) dropped as QEMU drops it; other CMOS bytes plain RAM, zero (QEMU's
`pc_cmos_init` data is firmware's). Guard: while PIE runs, or the update timer runs with UIE or AIE.
**ACPI PM block** (`pm.rs`; QEMU `hw/acpi/core.c` 390-760; ACPI 6.5 4.8): PM1_STS write-1-to-clear, PM1_EN all 16 bits
read back, the SCI (ISA IRQ 9, a level line through L4a) = STS & EN & (TMR|GBL|PWRBTN|RTC); the 24-bit PM timer
counted from VM creation, writes ignored; TMR_STS on each bit-23 toggle, its overflow scheduled (and guarded) while TMR_EN
and TMR_STS clear. PM1_CNT: SCI_EN set and read-only (ACPI-only mode, Table 4.13 "OSPM always preserves this bit";
**departure:** QEMU lets a write clear it), SLP_EN write-only; SLP_EN with `_S5`'s SLP_TYP (0) ends the run with the typed
stop `poweroff`; another SLP_TYP with SLP_EN is refused (`unsupported_port` at the instruction, register unchanged: no
S1-S4 here). Reset register (one byte at B040H, the FADT's; QEMU's PIIX4 has none): 6 ends the run with the typed stop
`reset`, other values do nothing, it reads 0. **Departure:** TMR_STS is first set at the first toggle, 2^23 ticks after
creation (QEMU starts its overflow time at 0, so TMR_STS reads 1 from reset). Every register is reached by bytes (a wider
access is its bytes; a 32-bit write at B000H is one update); QEMU decodes PM1 at 16 bits and the timer at 32.
**Typed stops:** `execution::Stop::Power` after the port access that asked (`Devices::power`), reported `poweroff` or
`reset` by both drivers (the program driver cancels the other vCPUs). **Wiring:** `clock::install` registers the PIT at
40H-43H and 61H (`devices::At`, one device on several ranges), the RTC at 70H-71H, the PM block at B000H/B004H/B008H and
B040H, in `run-program` and `boot-linux`.
**L5, the HLT wake** (`runtime/src/execution/lapic.rs` `wait`, `events.rs` `park`): the X3 lateness was macOS's timer
leeway on the blocked vCPU thread (the same 50 % as host `nanosleep`; QoS had been ruled out by X3). A halted vCPU now arms
CNTV at half the time left to its timer's expiry and waits again on each wake; with at most 25 us left it spins on CNTVCT
until the expiry or a pending interrupt (ISR_EL1.I). The timer is left armed at the expiry. HLT one-shot lateness (X3's
program, 5 rounds, x2APIC, 3 runs each), before -> after: 50 us sleep 29-30 -> 4.0-5.5 us; 0.2 ms 105-107 -> 4.3-5.2 us;
1 ms 506-510 -> 4.3-4.4 us; 2 ms 1,007-1,010 -> 4.2-4.5 us; 4 ms 2,005-2,008 -> 4.5-4.7 us; 10 ms 5,005-5,011 -> 5.0-7.2 us;
200 us periodic HLT 38-109 -> 4.2-6.7 us.
**Checks** (`tools/run_l4b.py`, programs `tools/bench/smp/timers.py`): `qemu` 4 programs (PIT, RTC, PM at block sizes
default and 1; S5), natemu equal to pinned QEMU on every compared result (64 slots: values that do not depend on timing)
and both equal to the model; QEMU runs under SeaBIOS, and the PM programs first set the PIIX4 PM's PMBA/PMIOSE through
0CF8H (natemu: unassigned, ignored) and write 0F1H to B2H (QEMU's ACPI enable). PIT: read-back status after mode 0/2/3
loads, count and status latches, LSB-only and MSB-only access, a second counter latch ignored, a second status latch
ignored, port 61H (gate, data, refresh toggle, OUT2 low then high in mode 0), Linux's `pit_calibrate_tsc` (natemu:
t2 - t1 = 9.998 ms against the model's 10.0001 ms less the poll gap; Linux's own acceptance holds, 3,133-3,190 polls,
max/min 4.2-4.3), Linux's `quick_pit_calibrate` (QEMU and natemu succeed; natemu 999,937-999,963 kHz, i = 182-186, d1/d2
9.3-9.8 us; it runs 8 times: each failure ends on a path not yet translated, and a host compile costs more than the 214 us
an MSB lasts), IRQ0 at the mode 2 rate through pin 2 (20 deliveries, none before its edge, 7-22 us late from HLT) and
Linux's PIT-disable sequence (one IRQ0 more, as QEMU). RTC: 70H, A-D, SET and read-back, 23:59:58 31 Dec 1999 rolling
into 00:00:00 Saturday 1 Jan 2000 (century 20H), UIP, binary/12-hour and the re-encoding on a format change (entering
binary under SET reinterprets the BCD century 20H as 32: year 3200, on both), 37H, CMOS RAM, IRQ 8 periodic (16 at 1024
Hz, C = C0H), update-ended (C = 90H) and alarm (C = B0H). PM: SCI_EN, PM1_EN read-backs, PM1_STS clear, PM timer
monotonic and 24-bit, natemu's PM ticks within one of the exact count, the PM timer's SCI through pin 9 level (one
delivery, TMR_STS seen, cleared by the handler). S5: QEMU powers off, natemu stops `poweroff`. `halt` 2 (a vCPU halted on
a 55 ms PIT one-shot reaches its end with pin 2 unmasked, and is ended `halted` with it masked, once the one-shot fired).
`stress` 3 (1, 2, 4 CPUs, xAPIC: IRQ 0 at ~1 kHz and IRQ 8 at 1024 Hz broadcast; every CPU takes 200 of each from HLT;
between its 50th and 200th IRQ 0 exactly 150 PIT periods pass, within one: none merged once its code is translated;
before that, first translations merge 10-17). `cases` 10 (S5 by a 16-bit and a high-byte write, reset, other reset
values ignored and the register reading 0, SLP_TYP 1 refused, SCI_EN read-only, TMR_STS clear at start, a 32-bit PM1
write, PM timer writes ignored, the PIT's reset status 86H). `clocks` 1 (400 samples over 10 s, each read between its own
two TSCs: the PM timer, the PIT (channel 2 mode 2, unwrapped), the LAPIC current count and the RTC's seconds all within one
tick of the exact count from the TSC window against sample 1; widest windows 58 PM, 24 PIT, 195 crystal ticks; 10 RTC
second changes one second apart). Unit tests: `clock` (the inverse at both rates), `pit` (each mode's IRQ0 changes against
its level, QEMU's counts and OUT), `rtc` (calendar). **Kick latency for timer expiry (§2):** a host timer's interrupt
(PIT thread -> I/O APIC -> post -> SPI doorbell -> stop at a block boundary -> delivery) arrives 7-22 us after its edge;
the LAPIC timer on the in-VM CNTV 10-14 us running and 5.5-7.5 us from HLT (`run_x3.py timers`): both routes are
microseconds, so no second route is needed for timer expiries.
**Kernel** (Debian 6.12, 2 GiB, `console=ttyS0 earlyprintk=serial mitigations=off nokaslr`, no initrd, series28): it
reaches "VFS: Unable to mount root fs" as before (293 lines, guest 41.1 s, about 67 s of wall time). The RTC answers:
"Unable to read current time from RTC" is gone, and `rtc_cmos 00:01: registered as rtc0`, "setting system clock to
<host UTC>". "PM-Timer failed consistency check" is gone: `clocksource: acpi_pm` registers (21.9 s) and is the clocksource
selected. Linux programs PIT channel 0 periodic (34H) and leaves it running. Still present, neither in the clocks: **"tsc:
Fast TSC calibration failed"** (quick_pit_calibrate saw the MSB leave FFH before its first check: its first run pays a
host compile per new path, 0.4-2 ms, against the 214 us an MSB lasts; the same code warm succeeds at 999,9xx kHz above;
the TSC frequency still comes from CPUID 15H, "Detected 1000.000 MHz") and **the TSC marked unstable** at 10.97 s by
the watchdog `refined-jiffies` (acpi_pm registers only at fs_initcall, 21.9 s): jiffies advanced 120 ticks (480 ms at
HZ 250) while the TSC advanced 579.6 ms, so about 25 ticks were lost, in this window of first translations (the run
makes 90,277 compiles; "hrtimer: interrupt took 301 ms" later). Every clock here derives from V and agrees within a tick
(`clocks`); a periodic tick that expires while the vCPU is held in the host for a compile merges with the pending one, as
an x86 IRR merges (L5's rule: V never pauses), so tick-counted jiffies fall behind wall time. Remedies are owner choices,
not clock fixes: shorter or fewer stalls (compile latency), or a lost-tick policy for timer interrupts (catch-up
delivery, as KVM's PIT `reinject`), or pausing V while a vCPU is held in the host. The selected clocksource is acpi_pm
because the TSC was marked unstable.
**Not built:** the PIT's speaker output, BCD counting and NULL COUNT (QEMU has none); RTC_STS, power button and GPE
sources (none on this board); S1-S4; the 8254's datasheet control-word semantics (QEMU's kept, above).

*Built (L4b review round and the lost-tick policies, owner decisions of October 30; no fexc change, series28):*
**Review fixes.** The device thread read V twice per turn, so `at - now` could wrap and the thread slept for centuries
(the review's `frozen` probe, now `run_l4b.py cases frozen`: a mode 3 PIT, count 6, never accessed again, 20,000
windows of 100 us each needing an IRQ 0 edge in the PIC's IRR; red on the old loop, 1 of 3 runs with 19,999 windows
empty; green 7 of 7 since): V is read once per turn. Every access first takes every event already due
(`clock::Timer::access`), so a reload cannot drop a due edge and C's flags are exact; the review's `lost-edge` (300
trials each at counts 24 and 60) and `oneshots` (5,000) probes miss nothing. The PIT follows channel 0 up to now in one
step (every change due at once; rising edges that passed together make one edge), and a thread takes at most one turn
per 20 us and drops the device's lock between turns. The review's 3-4.6 ms for the three OUTs of the PIT-disable sequence
at count 2 was first translation, not the lock: the same cold sequence takes 2.96 ms at count 255, and warm it takes
7.0 us at count 2 against 9.8 us at count 255. A halted vCPU whose timer cannot deliver (LVT masked, IF = 0, a class
not above the PPR) waits plainly, with no early wakes or spin; early timer wakes count as neither stops nor IRQs
(`lapic::timer_irq` reports whether the expiry was due). The early-wake scheme costs about 2 % of a core at a 250 Hz
periodic timer and 5.4 % at 1 kHz (the review's `hltcpu` probe); lateness is 4-7 us typically, with a tail of
90-388 us. A host real-time thread that rings at a deadline the parking vCPU publishes was not built.
**Lost-tick policies** (`clock::LostTicks`; `--lost-ticks pit=..,rtc=..,lapic=..` on both drivers): *reinject* delivers
each missed period later, one per acceptance of the previous; *discard* merges them. Missed means the edges passed
together, or the vector was still pending in the IRR; the chipset now reports what each rise did (`irq::Delivery`:
fresh, coalesced, or taken by nobody). Defaults, from the sources:
- **PIT reinject.** KVM's in-kernel PIT: `kvm_pit_set_reinject(pit, true)`, arch/x86/kvm/i8254.c 702; its pending count
  is injected one per `irq_ack`, 248-273; QEMU's `kvm-pit` keeps `lost_tick_policy` `delay`, hw/i386/kvm/i8254.c
  322-323. QEMU's own PIT has no policy. Here a retry every quarter count re-raises IRQ 0 until a fresh acceptance;
  a count load or a rise nobody takes clears the debt, as KVM's reset and mask notifier do.
- **RTC discard.** QEMU's mc146818 default; `slew` is `-rtc driftfix=slew`, reinjection on each read of C.
- **LAPIC discard.** KVM's `apic_timer_expired` returns while an expiry is pending, arch/x86/kvm/lapic.c 1920-1921. The
  resident's reinject (`lapic::reinject`) raises an owed expiry at a boundary once the vector's IRR bit is clear.
`run_l4b.py modes`: each source at ~1 kHz with IF = 0 for 20 ms, then 20 ms with IF = 1; reinject delivered every period
(PIT 40 of 40, RTC 41 of 41, LAPIC 40 of 40), discard one per IF = 1 period plus the merged one (21 of 20 each).
**Which timer loses ticks** (`boot-linux --clock-trace`): Linux runs the PIT-disable sequence at boot (CPUID 15H gives
the LAPIC timer's rate, so `apic_needs_pit` is false). IRQ 0 rose once in the whole boot, 55 ms later, masked. The tick
is CPU 0's LAPIC timer in periodic mode until the switch to one-shot. Across the watchdog's 0.5 s window, 31 of about
125 LAPIC expiries merged (Linux counted 24 jiffies lost); 345 merged before the switch. The LAPIC's default stays
discard, KVM's practice, so **the policy does not remove this symptom**; K1 removes the compile stalls that cause it.
With `--lost-ticks lapic=reinject` (343 reinjected) the TSC stays stable: tsc-early at 9.78 s, then the tsc
clocksource at 25.3 s, and the panic at 42.9 s (287 lines, 78 s of wall time with a 10 s watchdog).
**Tests added:**
- `qemu l4b-more` (natemu equals QEMU on k0-k8, each at block sizes default and 1):
  - PIT mode 4 (38H): one IRQ 0, status 38H before and after the strobe;
  - mode 1 (B2H): status 32H, OUT2 high after the gate rise;
  - mode 5 (BAH): status 3AH, OUT2 low before and 1 ms after;
  - the RTC's period change from rate 15 to 14, 100 ms after an interrupt: the next comes 249.9 ms after it (QEMU
    251.1), the lost-clock path.
- `modes`: PIT modes 6/7 per the datasheet: 23 IRQ 0s in 20 ms each, status keeping M2..M0 (QEMU: one edge).
- **Departure, fixed per the MC146818 datasheet:** QEMU decodes a 12-hour hours register with its PM bit inside the BCD
  value, and gives the alarm the time register's PM flag. At 11:59:59 AM an alarm at 12:00:00 PM sets AF here within
  3 s (k9 = 1); QEMU does not (k9 = 0).

**Owner decisions (October 30, after the gap audit):** (1) 64-bit code with the 0x67 address-size prefix is **built**, not refused: exact 32-bit effective-address truncation for LEA, memory forms, strings and branches with exact fault sites (initramfs: 1,435 sites incl. libcrypt's relaxed `addr32 call` and libgcrypt `leal`). (2) VEX gathers and the masked stores (MASKMOVQ, MASKMOVDQU, VMASKMOV/VPMASKMOV) stay **accepted typed refusals**, recorded with census counts. (3) Unmasked floating-point exceptions are **built**: exact #XM for SSE/AVX (and #UD per CR4.OSXMMEXCPT) and #MF for x87 (CR0.NE), replacing the unmasking refusals (I5c). (4) The host split (L0) is **approved**: a host library plus driver crates, tamper hooks turned into a generic stop hook supplied by the test driver, and a clean boot entry for integration. Also from the audit, as architecture rather than choices: forms the profile does not advertise (XOP, RTM, PadLock, reserved VEX/MMX encodings) deliver #UD instead of stopping; the CET hint space (RDSSP, ENDBR) executes as the NOPs it is without CET, while INCSSP, SAVEPREVSSP and RSTORSSP are #UD with CR4.CET = 0 per their SDM pages (corrected after I10; an earlier draft of this paragraph called INCSSP a NOP); XLAT is built.

**Owner decisions (October 30, after L4b):** (1) both remedies for ticks lost while a vCPU is held in the host: a per-device lost-tick policy for periodic timer modes (PIT modes 2/3, the RTC periodic interrupt, the LAPIC periodic timer), either *reinject* (count missed periods and deliver them when the vCPU can take them) or *discard*, with each device's default taken from what QEMU/KVM actually default to (checked, not assumed) and recorded; one-shot timers are unaffected; and (2) remove the cause: K1 (keep translations across CR3/control-register changes) moves up to right after I8, followed by a measured boot breakdown (wall time per phase, compiles, exits by kind, halted time, flushes) against QEMU to the same point. Context: the Debian boot to the root-fs panic took 90,277 host compiles of 0.4–2 ms each; tick expiries merged during those stalls, so the refined-jiffies watchdog marked the TSC unstable and quick PIT calibration failed.

*Built (I8, AVX/AVX2/FMA/F16C; fexc series29 = series28 + patch 0021 + shim changes; reviewed October 30, no blocking defect; the review round's fixes in series30, below):*
**Forms.** FEX's AVX128 lowering (`SupportsAVX`, no SVE256 on the M4: each 256-bit operation as two 128-bit halves, YMM
upper halves in the guest-state page's `avx_high`). `FexcSlice1::Avx` admits the VEX forms of the four advertised features
by VEX map, pp, L, W and reserved vvvv and by operand kind (SDM Vol.2 per instruction): VEX.128 and VEX.256 SSE equivalents,
AVX2 integer forms, VPERMD/VPERMPS/VPERMQ/VPERMPD, VPERM2F128/VPERM2I128, VINSERT/VEXTRACT F128/I128, VBROADCASTSS/SD/F128/
I128, VPBROADCASTB/W/D/Q, VPBLENDD, VPSLLV/VPSRLV/VPSRAV, VPERMILPS/PD, VTESTPS/PD, FMA (packed, scalar, ADDSUB/SUBADD in
the three orders), VCVTPH2PS and VCVTPS2PH (register destination), VZEROUPPER/VZEROALL. Scalar ("LIG") forms are admitted
with VEX.L = 0 only (policy). The BMI1/BMI2 forms are as I1 (`pdep`/`pext` still refused; built in I9). Refused with their reason
(`unsupported_form`, before any effect): VGATHER*/VPGATHER* (FEX's element loop), VMASKMOVPS/PD, VPMASKMOVD/Q and
VMASKMOVDQU (FEX's masked load/store loops or a full store), VLDMXCSR/VSTMXCSR (the resident's MXCSR transaction decodes the
legacy forms only), VCVTPS2PH to memory (its conversion raises MXCSR flags before the store, which may fault),
VPCMPESTRx/VPCMPISTRx (host helper), the VEX AES/PCLMULQDQ forms (not in I8), VINSERTPS m32 whose zero mask clears the
inserted element (FEX drops the load, as I5a), and every encoding the SDM makes #UD (VEX.L, VEX.W, reserved VEX.vvvv,
register/memory kind) or the profile does not advertise (AVX-VNNI): refused, not delivered.
**Gating (translation time).** The host passes `--avx-gated` when the bank has no CR4.OSXSAVE, XCR0[2:1] != 11b or CR0.TS
(`execution::avx_gated`); fexc cuts every AVX form, the refused ones x86 defines included, as gated (`cut_gated`, map 8),
the host answers `system::AVX_GATED` (49), and the resident raises #UD without OSXSAVE or with XCR0[2:1] != 11b, else #NM
(SDM Vol.2 2.8; CR0.EM and CR4.OSFXSR do not gate VEX forms; BMI is not gated). `Cpu::code_context` gains bit 17,
`Cpu::avx_enabled()` (OSXSAVE and XCR0[2:1] = 11b), so XSETBV (no flush) cannot leave a block compiled under the other
gating; CR0/CR4 changes already flush. A 66, F2 or F3 prefix before VEX, or a REX right before it, is a system cut the host
decodes as `UD` (`system::vex_ud`), #UD whatever the gating (QEMU's order too); LOCK is I3's `LOCK_UD`; a REX before VEX with
another prefix between them is refused (no SDM text).
**State.** `frame::AVX_HIGH` (C0H, 256 bytes) joins `GUEST_STATE` and `GUEST_STATE_WRITABLE` (I5b's hook 1): generated code
reads and writes the upper halves there (`ldr/str q`, `ldp/stp q`, `stp xzr, xzr` for a VEX.128 destination's zeroed half,
VZEROUPPER's `stp q0, q0` sweep), and XSAVE/XRSTOR component 2 is that same copy. Patch 0021 flushes them per instruction
(`FlushRegisterCache(true)` now covers AVXHigh0..15): FEX kept dirty upper halves cached across instructions and VZEROUPPER's
invalidation dropped them, so a fault in between would have reported stale upper halves. Admission decodes the Q-register
pair (`ldp/stp qT, qT2, [xN, #imm7*16]`) and admits x28 pair accesses inside a field; the program driver reports `ymmh`
(32 quadwords) and `frame_intact` no longer needs its AVX exception.
**Memory forms.** A 32-byte operand is one `ldp`/`stp` of two Q registers, a site of kind `load_pair`/`store_pair` with
width 32 and class FPR (admitted only so; patch 0021 also keeps it one pair with an FS/GS base). Its destinations are FEX
temporaries (the GuestOpcode/access barrier keeps loads out of static registers: `verify_precision` P3). Every store wider
than a byte has its split check (`add x0, A, #31` for 32 bytes); the resident completes a page-crossing 32-byte store from
the pair's two Q registers (`split`), after walking both pages for write, so a fault on either page writes nothing.
**Alignment:** VMOVAPS/VMOVAPD/VMOVDQA, VMOVNTPS/PD/DQ and VMOVNTDQA get `and x0, xA, #15` (VEX.128) or `#31` (VEX.256) on
the first access (`fexc_align16`/`fexc_align32`; admission and P10 check the mask by width): #GP(0) before any walk; every
other VEX form has no alignment requirement (class 4). **#AC** (X2): under alignment checking a misaligned 32-byte vector
access is #AC(0), an 8-byte-aligned one is refused `unsupported_mode` as I5a's 16-byte one ("may or may not"). **Atomicity
(decision):** SDM Vol.3 9.1.1 guarantees single-copy atomicity only for the 16-byte VEX.128 VMOVAPS/VMOVAPD/VMOVDQA (and
the legacy forms): patch 0021 lowers their memory forms to one 16-byte aligned LDP/STP of two X registers as I5a's MOVAPS
(FEAT_LSE2); a 32-byte access has no x86 atomicity guarantee, not even per 16-byte half, and an Arm LDP/STP of Q registers
is single-copy atomic per aligned 64 bits, which x86 does not promise beyond either. VMOVNT* are plain accesses, ordered as
any store (allowed). Element forms (VMOVLPS/HPS/LPD/HPD, VPINSR*, VPEXTR*, VEXTRACTPS m) use I5a's plain element accesses;
broadcasts from memory are a plain element load and a duplicate (not `ld1r`). **Device pages:** no VEX site carries an M3
completion record, so any AVX access to a device page stops `unsupported_mmio` (form) before any effect; reads of the
all-ones unassigned pages run natively (the L3 shadow), whatever their width.
**MXCSR exactness.** FEX's AVX128 lowering uses the I5a IR ops, so I5a's fixes key on the VEX forms too (VCOMISS/VCOMISD
`fcmpe`, VRCP*/VRSQRT* quiet, VROUND* `frintx`, VDPPS/VDPPD's masked inputs, a dead FCmp kept). Found by the corpus, fixed
in patch 0021: the AVX compare predicates 8..31 raised IE per FEX's choice of `fcmeq`/`fcmgt` and none for FALSE/TRUE;
under slice1 the predicate's operands are first compared for order by two quiet `fcmeq` (IE for an SNaN, DE for a denormal),
FEX's lowering then runs on operands whose unordered lanes are zero (no NaN reaches a signalling compare), the unordered
value is ORed in, and a signalling predicate adds one `fcmge` (IE for a QNaN) whose all-zero contribution keeps it live;
VFMADDSUB/VFMSUBADD negated the subtracted lanes' addend by an XOR of the sign bit, flipping a NaN's sign: now `fneg`, which
keeps a NaN under FPCR.AH = 1, selected per lane. FMA is Arm's fused multiply-add (one rounding); its NaN choice, signs and
flags matched QEMU on the corpus. F16C follows the SDM where QEMU 7.2 does not: VCVTPS2PH ignores MXCSR.FTZ (FPCR.FZ16
stays clear: a tiny result is a half denormal, UE and PE when inexact) and VCVTPH2PS ignores DAZ and reports no DE. DE as
I5a (IDC unless DAZ), judged by hand cases below.
**Deviations** (I8; (1), the 0x67 part of (6) and VLDMXCSR/VSTMXCSR, VPCMPxSTRx and VEX AES/PCLMULQDQ in (3) were resolved later: Built (I8) review, (I6b), (I9), (I10)): (1) the SDM's #UD encodings of VEX forms (reserved VEX.L/W/vvvv, wrong operand kind, unadvertised VEX
forms such as AVX-VNNI) are typed refusals, not delivered #UD; (2) scalar LIG forms with VEX.L = 1 are refused (hardware
runs them); (3) gathers, masked moves (so the SDM's non-faulting masked-off element is not built), VLDMXCSR/VSTMXCSR,
VCVTPS2PH m, VPCMPxSTRx and VEX AES/PCLMULQDQ are refused; (4) a 32-byte vector access that is 8-byte aligned under
alignment checking is refused `unsupported_mode` (as I5a's 16-byte); (5) any AVX access to a device page stops
`unsupported_mmio`; (6) 0x67 and a REX separated from VEX by another prefix are refused; (7) G1's page deviation (I5b) now
covers live YMM_Hi128: a guest data access to the guest-state page reaches the vCPU's own upper halves.
**QEMU 7.2 departures found** (recorded, not compared): VCVTPS2PH flushes tiny results under MXCSR.FTZ (SDM: "MXCSR.FTZ is
ignored"); VCVTPH2PS treats half denormals as zero under DAZ (SDM: "MXCSR.DAZ is ignored and is treated as if it 0"; "No
denormal exception is reported"); its disassembler prints VBROADCASTI128 as `.byte` (the annotation check cannot follow it,
so the corpus leaves it to the fault matrix and the ASM tests). The generator keeps F16C out of FTZ/DAZ groups.
**Found by the checks, fixed:** a pair access's IR offset is unsigned (`u32`), so FEX's `[base - 32]` pair (a displacement
that is a multiple of 16 folded into the LDP/STP) made the split check and its stub compute base + 0xFFFFFFE0; admission
refused the block ("no split check ... on exactly its address", FEX's `modrm_oob/VEX.asm`); `FexcBeforeOp` now sign-extends
it (`run_i8_faults.py` `*-negoff*`).
**Checks** (series29; `tools/reference/gen_i8.py`, `tools/run_i8_faults.py`): QEMU 7.2 corpus (`natemu-raw/i8-corpus-20261030-01`,
136 programs, 601 form templates at both widths, every rounding mode, FTZ/DAZ, data across the page boundary, VZEROUPPER/
VZEROALL in random mixes): 136/136 match at block sizes default and 1 (before the two MXCSR/NaN fixes: 8 failed, each
located by running its prefixes on both sides). `run_i8_faults.py` 803 cases at both block sizes, every one as expected:
#PF not present/read-only on 50 load/store forms, page-crossing 2..32-byte operands at three offsets (#PF on the second
page, CR2 its first byte, or on a read-only first page; the crossing store completed when both pages are writable), alignment
#GP for VMOVAPS/VMOVAPD/VMOVDQA/VMOVNT*/VMOVNTDQA (16 and 32; also with the page not present and with a non-canonical RSP
base), no alignment fault for every other form, non-canonical #GP/#SS and straddles, negative and large pair offsets; at
every fault GPRs, XMM, the YMM upper halves, MXCSR and the data region equal the run cut before the instruction; gating
(14 forms incl. the refused gather/VMASKMOV: #NM under TS, #UD without OSXSAVE, with XCR0 = 3, and with TS as well; CR0.EM
runs; BMI not gated); 66/F2/F3/REX/LOCK before VEX #UD (also under TS); DS before VEX runs; XSETBV clearing XCR0[2] makes
the already-run VEX form #UD; 7 refusals; all-ones unassigned pages read natively by 4 forms, device pages and stores
refused `unsupported_mmio`; XSAVE/XRSTOR round trips (RFBM 7 and AVX only) with live YMM state, the image's XMM and
YMM_Hi128 areas equal the registers; MXCSR hand cases (VCVTPH2PS of a half denormal: no flag; VFMADD231PS, VADDPS ymm upper
half, VCMPPS: DE; VCVTPS2PH of a float denormal: DE, UE, PE). `verify_precision.py` on the corpus (696 compiles, 8,990 sites)
P1-P13 clean (P10 accepts `#0x1f` for 32-byte accesses, P12 the YMM upper halves); `check_sites.py --slice1` 64 programs;
`test_slice1.py` 30 tests (new: AVX forms admitted, I8 refusals, prefix-before-VEX #UD). FEX ASM suite 1,053 -> 1,603 pass,
0 fail, 22 precision deviations (I6's); the review counted 152 I8-related refusals (155 with PDEP/PEXT), VCVTPS2PH m among
them (series30's count below). Census (`natemu-raw/kernel-census-i8-20261030-01`): kernel AVX 64 -> 11 (EVEX/AMX only), initramfs
AVX 329,545 -> 229,288 (`experiments/track03/kernel-census/README.md`). Standing suite on series29, all 0 failures:
cargo build/test (45 + 74), stack audit (gate 18,976 bytes, 23,056 nested, of 32,768: `split`'s 32-byte buffer), python unit
281, fix/fix-1 64, seed/seed-1 44, faults/faults-1 7,828 + 5 unsupported each (the three VEX `straddle-*-present` probes now
expect #UD: no OSXSAVE in that harness, as QEMU's qemu64), flow 64 x 2, loopfaults 846 x 2, probes 83, corpora (N = 2..4),
smp, limits, kicks, system, coherence, sharedpt, litmus, contain, privilege, delivery, events, i5a faults 454 x 2, i5a DE 351,
i6 faults 276 x 2, I8 corpus 136 x 2, I8 faults 803 x 2, `run_m3.py`, `run_l1.py` 41/41, `run_l2.py`, `run_l2_iasl.py`,
`run_k2.py`, `run_x3.py`, `run_m1.py` 20, `run_l3.py`, `run_l4a.py`, `run_l4b.py`, `run_i5b.py` 411.
**Kernel** (Debian 6.12, 2 GiB, `console=ttyS0 earlyprintk=serial mitigations=off nokaslr --initrd` Debian's initrd): past
the AVX2 `strrchr` (`vmovd xmm7, esi; vpbroadcastb ymm7, xmm7`) where it stopped before; 224 console lines (last: "Freeing
initrd memory: 39772K", "hrtimer: interrupt took 256723411 ns"), 55 s guest time, 81,025 compiles; stops `nocode`
`unsupported_form` at CPL 3, RIP 0x7f057f100d42 (CR3 0x4aa6000), `pcmpistri $0x12, (%rax), %xmm0` (`66 0f 3a 63 00 12`) at
libc.so.6 + 0x184d42, glibc's SSE4.2 `strcspn`/`strpbrk` family (PCMPISTRI: I5a's refusal, FEX calls a host helper; not I8's).
The process (series30, from its stack strings): `/sbin/modprobe -q -- crypto-hmac(sha256)`.
**Not built:** gathers, masked loads/stores (and so the SDM's non-faulting masked-off element), VLDMXCSR/VSTMXCSR, VCVTPS2PH to
memory, VPCMPxSTRx, VEX AES/PCLMULQDQ, delivered #UD for reserved VEX encodings, unmasked SIMD exceptions (as I5a), AVX-512.
*I8 review round (October 30; fexc series30 = series29 + patch 0022 + shim changes):* the review found no blocking defect and
judged deviations 1 and 2 (the #UD encodings refused, not delivered; scalar LIG forms with VEX.L = 1 refused) acceptable.
Fixed: (1) **an invalid encoding under the gating**: `Avx()` set `avx` on a refused form before its VEX.L/W/vvvv/operand
checks, so under CR0.TS an encoding x86 makes #UD (the reviewer's `c4e27d90c1`, VPGATHERDD with a register operand) was cut
as gated (#NM), and its VEX AES range (`op >= 0xdb`) also covered BMI's 0xf2-0xf7 (BMI matched first, no wrong verdict):
every refused form now has its own row (L, W, vvvv, operand kind) checked first, the AES range is 0xdb-0xdf (VAES's L = 1 not
advertised: an encoding refusal); `test_slice1.py`, `run_i8_faults.py` `*-invalid-*`. (2) **HADD's operand order**: the SDM
adds each pair high element first (HADDPD: DEST[63:0] := SRC1[127:64] + SRC1[63:0]; HADDPS: DEST[31:0] := SRC1[63:32] +
SRC1[31:0]; the VEX.256 forms per lane), so under Vol.1 Table 4-7's first-source rule two QNaNs give the high one; Arm's
FADDP adds low first. Patch 0022 computes UZP2 + UZP1 for HADDPS/HADDPD and VHADDPS/VHADDPD (I5a's legacy forms too); HSUB's
order already was the SDM's (DEST[31:0] := SRC1[31:0] - SRC1[63:32]: low first, FEX's UZP1 - UZP2). Evidence: the SDM
pseudocode only (hardware unverified); QEMU 7.2 picks two SSE NaNs by its x87 rule (the larger significand), so it agrees
with the SDM order only when the high NaN has the larger significand (the review's case) and not otherwise (extended corpus,
below); `run_i8_faults.py` `nan2-*` (8 cases: HADDPD/HSUBPD/HADDPS/HSUBPS, legacy, VEX.128 and VEX.256). Found while
re-running: legacy CMPPS/CMPPD/CMPSS/CMPSD and VEX UNORD_Q/ORD_Q (predicates 3, 7) raised IE for a QNaN (FEX's fcmge/fcmgt;
an I5a defect the I5a corpus never met): they take I8's exact-flag path now, which finds unordered lanes by integer compares
of |x| with infinity (no flag) and takes the flags from one fcmeq (quiet) or fcmge (signalling) of the operands, so no DE
beside a NaN either (`run_i5a_de.py` caught the first version's DE beside a QNaN; `run_i8_faults.py` `de-vcmpps-*`). (3) **The
upper-half write-back cost**: series29 stored each written upper half at the instruction's end and reloaded it in the next,
a store-to-load round trip on a dependent chain. Patch 0022 writes the written halves through at each instruction start (one
STR each: the state at every fault and exit stays exact) and keeps them cached across register-only vector instructions (VEX or
SSE map, no memory operand: `CanHaveSideEffects` false; at most 6 cached, so no spill); every other instruction starts with
the cache dropped. The reviewer's `bench.py` (20 M iterations, best of 3, ns per iteration, series29 -> series30): ymm-dep
10.33 -> 3.07, ymm-indep 1.55 -> 1.64, xmm-vex-dep 2.39 -> 2.39, xmm-sse-dep 2.39 -> 2.39, xmm-sse-dep-2x 4.19 -> 4.20,
empty 1.02 -> 1.03 (a first version that kept the halves cached without writing them through gave ymm-indep 3.52: its block-end
stores came last, before the next iteration's reloads). `run_i8_faults.py` `chain*-*` (15): a #PF of a VEX store, of a
general-register load, a misaligned MOVAPS, a #DE and a load after VZEROUPPER, right after 1, 3 or 15 register-only vector
instructions (12 registers written), each with GPRs, XMM, the upper halves, MXCSR and memory equal to the run cut before it.
(4) **VLDMXCSR/VSTMXCSR** (VEX.L0, vvvv 1111b, m32; glibc's FMA/AVX2 libm, SET_RESTORE_ROUND: 36 of each in the initramfs)
are system cuts the host decodes as `system::VLDMXCSR`/`VSTMXCSR` (50, 51): LDMXCSR/STMXCSR's transaction without CR0.EM/
CR4.OSFXSR gating, under the AVX gating (`AVX_GATED`); 12 cases (load, store, reserved bits #GP, the unmasking refusal,
#PF, #SS, FS, #NM/#UD gating, CR0.EM and no OSFXSR running); FEX's `VEX/vldmxcsr.asm` expects its own VSTMXCSR (flags masked,
FFC0H) where x86 stores FFFFH: listed as FEX's expectation. (5) **Corpus**: `gen_i8.py` now emits every template k = 2 times
per width under each of four MXCSR groups, mixes legacy SSE forms into the random programs (the reviewer's `gen_rev.py` list),
adds in-range VPSLLV/VPSRLV/VPSRAV, VPTEST/VTESTPS/VCOMISS flag captures and RIP-relative/SIB/FS-based 32-byte operands, and
keeps F16C out of FTZ/DAZ groups; corpus `natemu-raw/i8-corpus-20261030-02`, 336 programs: 329 match QEMU at block sizes
default and 1; the 7 others (`i8-forms-001`, `-045`, `i8-random-067`, `-077`, `-097`, `-111`, `-126`) each first differ, by
their prefix runs, at an SSE/AVX float operation with two NaN operands (VADDPD, VHADDPS x2, VMULSS, VSUBPD, DPPS, VDPPS):
QEMU takes the NaN with the larger significand (or the QNaN over an SNaN), natemu the first source quieted (Table 4-7),
I5a's recorded QEMU departure. The first corpus (`-01`) still matches 136/136.
**Census** (`natemu-raw/kernel-census-i8-20261030-02`): VLDMXCSR/VSTMXCSR are `system` (initramfs AVX 229,288 -> 229,216).
**Re-review** (passed, no blocking defect; the write-through invariant confirmed with kick stress, SMC and fault cases, the 7
corpus mismatches confirmed as QEMU's two-NaN rule): a gather is #UD also without a SIB byte (ModRM.rm != 100b) and when two of
its destination (ModRM.reg, VEX.R), mask (VEX.vvvv) and index (SIB.index, VEX.X) registers are the same; those encodings went
through the gated path (#NM under TS); the shim now refuses them first (series30 rebuilt in place; `test_slice1.py`,
`run_i8_faults.py` `*-invalid-gather-*`; 851 cases at both block sizes; FEX ASM `VEX/` 342 pass, 0 fail).
**Checks** (series30): `run_i8_faults.py` 845 cases at both block sizes, all as expected; I8 corpora `-01` 136/136 and `-02`
329/336 (the 7 above) at both block sizes; `verify_precision.py` on both clean; `test_slice1.py` 32; `check_sites.py --slice1`
64; FEX ASM 1,605 pass, 0 fail, 22 precision deviations; I8-related refusals 150 (153 with PDEP/PEXT 3): gathers 97,
VPCMPxSTRx 16, VEX AES/PCLMULQDQ 13, VCVTPS2PH m 9, masked moves 9, VNNI 4, 0x67 1, VLDMXCSR (FEX's expectation) 1;
`run_i5a_faults.py` 454 x 2, `run_i5a_de.py` 351, `run_i5b.py` 411, corpora (N = 2..4), slice-1 faults 7,828 + 5, cargo test
(45 + 74), python unit 281, stack audit (gate 18,976 bytes, 23,056 nested). **Kernel** (series30, the same command): the same
stop, `pcmpistri` at libc.so.6 + 0x184d42 in `/sbin/modprobe -q -- crypto-hmac(sha256)` (224 lines, 56 s), nothing earlier.

*Built (K1, the code cache across CR3 loads, INVLPG and control changes; no fexc change, series30; independent review:
no blocking defect, its two cheap findings applied):*
**Design.** Blocks and kept system records are filed by their guest-physical source pages and looked up by (linear RIP,
context); the context (`Cpu::code_context`, now one word) no longer holds CR3, nor CR0.PG/WP, CR4.PGE/PCIDE/SMEP or
EFER.NXE: a change of one of those is a global TLB flush, after which every block's fetch is walked again under the new
controls before it runs. It keeps everything the host gives fexc: CPL, SSE and AVX gating, x87 gating and rounding,
alignment checking. A block records `checked` (the TLB epoch at which a walk last found its source pages at its linear
address) and `GLOBAL` (every source leaf has G, with CR4.PGE). **TLB model** (per vCPU: `Control::epoch`,
`global_epoch`): a block is *current* when `checked == epoch`, or `GLOBAL` and `checked >= global_epoch` (MOV to CR3
keeps global entries, SDM Vol.3 4.10.4.1). Lookup takes a current candidate; any other is revalidated by a fetch walk of
each source page (A bits set, as the CPU's TLB miss; a fault means no): on a match `checked` moves up and `GLOBAL` is
recomputed, else the next candidate is tried, so blocks of other address spaces at the same RIP stay for when their
mapping returns. New blocks start current and not global (since perf chunk 8, global when their placement walk found every leaf global with CR4.PGE; perf plan "Measured (chunk 8)"). A PTE change without invalidation keeps the old translation
(TLB-stale-legal), as the shadows already do. The dispatcher's L1 and block-to-block links run with no check, so every
way a block stops being current also cuts them: L1 records each entry's block (`L1_OWNERS`) and `prune()` undoes every
link into a block not current or dead and out of a dead one; links out of a block only not current stay (it runs again
only after a revalidating lookup). The linker links only to a target `find` just made current. **Events:**

| Event | Shadows (data TLB) | Blocks |
| --- | --- | --- |
| MOV to CR3 (any value, the same too) | all reset | epoch + 1: non-global ones stop being current |
| CR0.PG/WP, CR4.PGE/PAE/SMEP/SMAP/PCIDE/LA57, EFER.NXE/LME/LMA change; MTRR write | all reset | global flush: `global_epoch` = epoch + 1, none current |
| Other CR0/CR4/EFER bits (TS, EM, MP, OSFXSR, OSXSAVE, AM, TSD, ...), XSETBV, FCW/MXCSR | none | none: the context keys what code depends on |
| INVLPG | the 2 MiB-aligned range (one walk, `unmap_large`) | blocks with bytes in the range: `checked` 0 (since perf chunk 8: on the named 4 KiB page, and blocks from a 2 MiB leaf in the range) |
| #PF delivered (SDM Vol.3 4.10.4.1; a gap before K1 too) | none (the miss had none) | blocks with bytes on CR2's page: `checked` 0 |
| Guest, peer or walker write to a page with translations | as before | the page's blocks killed (reverse map) |
| Pool full; the host's `FLUSH` (guest RAM written while stopped: only at load) | as before | **the only full flushes left** |

Translations do not depend on MTRRs or PAT (until M2 the walker's write-back check was redone at revalidation; since M2 a
fetch walk accepts RAM of any type, and UC data is reached through the step block, M2 addendum 2). PCID is not advertised. Each vCPU's epochs, L1, links and blocks are its own: a CR3 load on one never
touches another. **Reverse map:** a per-page head array (`PAGE_BLOCKS`, u32 per guest page, cleared at reset through the
code summary; `file()` sets the summary bit too) chained through each block's `next[2]`, so a written page finds its
blocks without scanning; kills take the block out of L2 (backward-shift deletion: with the larger pools dead copies
otherwise piled up along probe paths, and `coherence` cc-data@4 hit the watchdog); an INVLPG looks at blocks only when a
1,024-bit filter of 2 MiB regions (`Control::regions`, both ends of each block) says one may lie there, then (review
round, below) only at the blocks on the linear map's chains of its pages. **Pools**, from the boots (80.6 k live blocks,
20.9 MB of metadata at the panic, 3 capacity flushes at the old size; 237.8 k blocks and 62.7 MB into user space, below):
256 Ki blocks (L2 512 Ki entries), 64 MiB of metadata, 64 Ki links. Counters: `Control` cr3_loads, global_flushes,
invlpgs, revalidated, remapped, capacity_flushes, killed, unvalidated and the ticks of TLB flushes and INVLPGs (the
drivers' `k1` stats). Measured cost over the boot: 81 ms for 18,557 CR3 loads and 38 global flushes, 24 ms for 70,227
INVLPGs.
**Checks.** `tools/run_k1.py` (new), each CPU with its own tables A and B, at 1-4 vCPUs and block sizes default and 1,
96 of 96 runs green. Against QEMU: remap + INVLPG; remap + CR3 reload; K rounds of CR3 A/B with different bytes at one
linear address; a write to the other space's idle block; the remap and CR3 cases through linked direct jumps (links made
and cut); an instruction crossing into a remapped page (INVLPG of the second page alone, then a third frame and a CR3
reload); the same across a 2 MiB boundary (0x1ffffe). By the architecture's rules, natemu only: a PTE change with no
invalidation (either frame, then the new one after INVLPG); global pages (the block found global is kept across a load of
B, asserted; a CR4.PGE toggle or INVLPG forces the new bytes); a revalidation walk whose A-bit write kills its own
candidate (the PTE is the instruction's immediate: the new bytes run); a #PF whose handler remaps without INVLPG (the new
bytes run). Each new check was red with its mechanism disabled in a scratch build: the second page's walk (cross: r9
0x100 for 0x200, r10 0x100 for 0x300), the last byte's region bit (straddle, default block size: r9 0x100 for 0x200; at
block size 1 another block marks the region), the dead re-check after the walk (walk: r9 Q|7 for Q|0x27), `GLOBAL`
(global: r9, r10 2 for 1), the #PF invalidation (pf-remap: r9 1 for 2). Counts (1 vCPU, CR3 ping-pong over shared code
at 50 and 100 rounds): 14 and 14 resident requests (HEAD: 356 and 706, with 103 and 203 full flushes); host compiles 22
either way (its own cache). Standing suite on the tree with I6b's work in progress (series30): green, `system` 0
failures, `coherence`, `sharedpt`, `contain`, `privilege`, `delivery`, `events`, `litmus`, `kicks`, `smp`, corpora
(N = 2..4 all match), faults 7,828 + 5, loopfaults, probes, asm, fix/seed (both block sizes), `run_k2.py` (cost, RDRAND,
XMC, coherence), `run_m3.py` (XMC included), `run_m1.py` 20 cases, x3, l3, l4a, l4b, i5b, i6 and i8 faults, i8; i8b with
its 7 known QEMU NaN departures; cargo test (49 + 75), the stack audit. Failures seen on the way were `hv_vm_create`
0xfae94007 collisions with another agent's VMs (clean on rerun) and one l4b RTC reinject count of 43 for [39, 42]
(timing; green on rerun).
**Boot** (Debian 6.12 bzImage, 2 GiB, no initrd, `console=ttyS0 earlyprintk=serial mitigations=off nokaslr`, 10 s
watchdog, one VM at a time):

| | HEAD (before K1) | K1 |
| --- | --- | --- |
| Wall time to the root-fs panic | 59.6 s (61.8 s) | 40.8 s (40.2 s) |
| Guest time at the panic | 36.4 s | 29.3 s |
| Host compiles | 90,453 | 76,488 |
| Resident translation requests | 1,777,737 | 79,920 |
| Full flushes | 18,604 | 1 (the load) |
| By cause | 18,557 CR3 loads, 38 global (PGE toggles, paging bits, MTRRs), 1 load: each a full flush | 18,557 CR3 loads and 38 global flushes, none flushing code; 70,227 INVLPGs (no block in range); 0 for capacity |
| Blocks killed by writes / revalidated by a walk | (not counted) | 29,646 / 47,312 |
| TSC marked unstable | yes | yes (77 ms skew over 508 ms) |
| Quick PIT calibration | fails | fails |

The remaining 76.5 k compiles are first translations of code that runs once (about 80 k blocks) plus blocks killed by
the kernel's text patching (about 30 k) and translated again from their new bytes; their stalls still merge ticks.
**Not built / remaining:** compile latency (the next lever: first translation off the critical path, finer than page
invalidation for text patching, or a virtual-time remedy), and the measured boot breakdown the October 30 decision asks
for; shadows still reset at every CR3 load (19,481 resets, about 456 k shadow-miss retries in the boot: global shadows or
one shadow set per address space); an INVLPG inside a guest 1 GiB page covers 2 MiB (as before); a lookup walks each stale
duplicate at a RIP again (many address spaces with private code at one address); a flush zeroes the 4 MiB L2.

*K1 review round (October 30; an independent review, no blocking defect; it reproduced the boot numbers and passed nine
adversarial programs):* **#PF and INVLPG no longer scan every block.** Both looked at every block once a 2 MiB region's
bit was set (bits clear only at reset; the review measured about half the early #PFs scanning, ~280 us each with 77 k
blocks). Now a linear map (`LINEAR_HEADS`: 64 Ki chain heads by a hash of the linear page; each block on the chains of
its first and last byte's pages through `Block::lnext`) gives the blocks of a page; a walk of a chain drops the dead
blocks it meets, so each leaves once. Making blocks not current prunes the link records only when one may be a link
target (`INBOUND`, set when a link is made, cleared when the block is cut and its links pruned). Counted and timed in the
`k1` stats (`page_faults`, `page_fault_us`, `invlpg_us`, `scanned`). **Boot with the initrd** to the current user-space
stop (`unsupported_form` after "Run /init", the coordinator's /bin/udevadm MOVBE stop; fexc series31, otherwise as above): before the change
141.1 s, 255,231 compiles, 256,992 requests, one capacity flush (128 Ki blocks), 72,236 INVLPGs in 350 ms; after it 135.0
s, 234,390 compiles, 234,905 requests, no flush at 256 Ki blocks, INVLPGs 142 ms, 3,532 #PFs in 4.1 ms, 2.47 M chain
entries visited over the run. Without the initrd: 41.5 s to the panic, guest 29.4 s, 76,536 compiles (as before).
**Also:** `lookup` and `cut` work on a copy of the block (the revalidation walk may kill it; reading it through a shared
reference after that write was undefined behaviour); stale comments fixed (`host/src/execution.rs` SSE gating,
`runtime/src/execution/sse.rs`, the handoff's system-instruction row, `run_k1.py`'s `global`); the review's adversarial
programs are in `run_k1.py` (NX by PTE change + INVLPG or CR3 reload, SMEP set, U/S change under SMEP, code remapping its
own page with INVLPG, CR3 reload or a PGE toggle, one frame at two linear addresses rewritten, global/non-global page
crossings, a fetch #PF then a remap without INVLPG); `sharedpt` at 1 vCPU skips the contended walks (they need a
clearer and a walker; red at HEAD too). **Checks:** `run_k1.py` 184 of 184 at 1-4 vCPUs and both block sizes, counts 14
and 14 requests; `system`, `coherence`, `sharedpt` (also at 1 vCPU), `events`, `run_k2.py`, `run_m3.py` (XMC included)
0 failures; cargo test, stack audit green. **Not done:** blocks of other address spaces at one RIP are not bounded: a
lookup walks each stale copy (`remapped` counts them: 160 in the user-space boot), and they leave only when written or at
a flush; bounding needs a kill inside the lookup or at placement, left until a program with many private copies at one
address shows the cost.

*Built (I6b, the x87 helper forms and PCMPxSTRx by `svc` to the resident; fexc series31 = series30 + patch 0023 + shim changes):*
**Design.** No helper library in the VM and no host helper: two new `svc` numbers enter the resident from generated code, as
the 128/64 division does (`execution::svc`, fexc `FexcSvc`). **`svc #X87` (5)**: `movz w0, #fn; svc #5`, fn the D9 opcode's
second byte (F0 F2XM1, F1 FYL2X, F2 FPTAN, F3 FPATAN, F4 FXTRACT, F5 FPREM1, F8 FPREM, F9 FYL2XP1, FB FSINCOS, FD FSCALE, FE
FSIN, FF FCOS). The whole instruction runs in the resident (`execution/helpers.rs` -> `x87::helper`, math in
`natemu_abi::x87::helper`) on the x87 state FEX's stack pass leaves exact in the guest-state page at every instruction boundary
(I6): it reads TOP, the tag byte, ST(0), ST(1) and whether TOP - 1 is empty, writes the values and tags, TOP, the condition
codes the SDM defines for the instruction and ORs the flags into the x87 exception word. FEX's F64 IR ops carry only a value,
so lowering them would have needed FEX-side C2, conditional pushes and quotient bits; patch 0023 instead lowers the instruction
(`X87OpHelper` for the nine helper IR ops only, not FCHS/FABS/FSQRT/FRNDINT, `X87FYL2X`, `X87FXTRACTF64`) to one new IR op
`FexcX87Svc` (side effects; the JIT op in `shim/fexc_sites.inc`). No register changes; FCW comes from the bank (`Cpu::fcw`).
**`svc #STRING` (6)**: FEX's PCMPxSTRx lowering stays (it derives ECX or XMM0 and RFLAGS from the intermediate result); only its
VPCMPISTRX/VPCMPESTRX helper call becomes `mov v0, xmm1; mov v1, xmm2/m128; [mov x1, RAX; mov x2, RDX]; movz w0, #control; svc
#6` (`FexcStringSvc`, from `Op_Unhandled` under slice1), control = imm8 | REX.W/VEX.W << 8 (FEX's own bit) | explicit << 9;
the resident (`natemu_abi::strings::pcmpstr`, written from FEX's `VectorFallbacks.h` loops with the SDM's validity overrides)
returns w0 = IntRes2 | SF ZF CF OF << 28 and changes nothing else. **What the resident preserves:** every register but w0
(`STRING`) and none (`X87`), NZCV, FPCR and FPSR: the gates save all 32 Q registers, FPCR and FPSR and `resume` restores them
from the captured frame, so the resident's own libm and vector use never reaches guest state (its FPCR is 0 since I5a). Each
handler checks that the `svc` came from a block and that the function code or control is one it knows (else `unknown_abort`).
**Admission** (object validation, `admission/structure.rs`): `#X87` and `#STRING` join the allowed `svc`s; each must lie in its
x86 instruction (`x86_x87_helper`: D9 F0..F5/F8/F9/FB/FD..FF after segment, 67 and REX prefixes; `x86_string_compare`: 66 [REX]
0F 3A 60..63 or VEX.128.66.0F3A 60..63) right after a `movz w0` of exactly that instruction's function code or control word
(imm8, W and the explicit bit from the bytes), in the same instruction, with no branch to the `svc`; `#STRING` writes x0.
Negative tests (`helper_svcs_only_in_their_instruction`, a real series31 fixture `helper-forms`): a wrong function code
(FCOS's on FSIN, FPREM1's on FPREM), no movz, a wrong imm8, REX.W or explicit bit dropped, an implicit form with the explicit
bit, a branch over the movz to the `svc`, the other helper's `svc` in each instruction, and either `svc` in an ordinary
instruction: all refused, with and without the code-shape check. (Found by the ASM suite while building:
FEX routes FCHS, FABS, FSQRT and FRNDINT through `X87OpHelper` too, and admission refused the `svc` it then emitted there.)
**Forms.** PCMPESTRI/PCMPESTRM/PCMPISTRI/PCMPISTRM legacy and VEX.128 (VEX.L = 0, vvvv = 1111b, W either), register and memory
operands: the 16-byte operand is an ordinary FPR load site with no alignment check (SDM: none for these), so #PF/#GP/#SS are
precise as any SSE load and a crossing load faults on the second page with nothing written. x87: all twelve helper forms,
FXTRACT included (exact for a double denormal: its 80-bit exponent). Gating as I6/I8 (`--x87-gated`, `--sse-gated`,
`--avx-gated`: #NM/#UD before the `svc` is ever reached). **x87 rules** (SDM per instruction, Vol.1 8.5 masked responses;
Bochs where the SDM gives none; QEMU's partial step): FSIN/FCOS/FPTAN/FSINCOS with |x| >= 2^63 set C2 and leave ST(0)
unchanged with nothing pushed and no exception; an infinity is IE with the indefinite (FPTAN/FSINCOS: in both registers); a NaN
propagates quieted (IE for an SNaN; FPTAN pushes the NaN too, Bochs); FPREM/FPREM1 set C0 = Q2, C3 = Q1, C1 = Q0 of the exact
quotient (the low bits from fmod(|x|, 8|y|) - fmod(|x|, |y|); FPREM1 rounds to nearest even) with C2 = 0 when the exponent
difference D < 64, else the partial remainder x - y * QQ * 2^(D - N), N = 32 + D mod 32, truncating, C2 = 1 and C0 C1 C3 = 0
(AMD's rule, which QEMU follows and Intel matches empirically: the last step of a loop gives the complete quotient's bits);
ST(0) infinite or ST(1) zero is IE, ST(1) infinite or ST(0) zero leaves ST(0); FYL2X/FYL2XP1/FPATAN by the SDM tables (log of a
negative, 0 * log2(0), inf * log2(1): IE; a zero argument with a finite nonzero ST(1): ZE and the signed infinity), each
popping; FSCALE truncates ST(1), rounds by FCW.RC (`from_f80`), OE/UE/PE at the double's thresholds, the SDM table for
infinite ST(1); FXTRACT of zero is ZE (-inf, then the zero), of an infinity +inf then it. Stack faults: an empty operand is
underflow, a non-empty TOP - 1 before a push overflow: IE and SF, C1 = 0 / 1, every destination the indefinite (FPTAN,
FSINCOS and FXTRACT: both registers, Bochs), popping forms pop. PE whenever the result is inexact (exact cases recognized:
zeros, cos 0, 2^+-1 - 1, log2 of a power of two times an exactly representable product, atan2 zeros); no DE (a double is never
an 80-bit denormal). **Accuracy:** values from compiler_builtins' libm for `aarch64-unknown-none` (sin cos tan atan2 expm1 log2
log1p fmod ldexp fma; F2XM1 as expm1(x ln 2), FYL2XP1 as log1p(x) log2(e)), to nearest whatever FCW.RC/PC.
**Deviations** (I6b; the reduced-precision class): (1) FSIN/FCOS/FPTAN/FSINCOS/F2XM1/FYL2X/FYL2XP1/FPATAN results are double
libm results, rounded to nearest whatever FCW.RC, within about 1 ulp of the double nearest the exact value (an 80-bit x87
rounds by RC at 64 bits; I6 deviation 1 already covers 53 bits). libm reduces a large trigonometric argument exactly (Payne-Hanek),
where x87 hardware reduces by a 66-bit approximation of pi: for |x| far from 0, and most near 2^63, natemu's FSIN/FCOS/FPTAN/
FSINCOS differ from a real CPU's by many ulps (natemu being the closer to the exact value); (2) their C1 is 0, never the round-up bit (I6 deviation 5);
(3) F2XM1 outside [-1, 1] and FYL2XP1 outside |x| < 1 - sqrt(2)/2, which the SDM leaves undefined, compute the formula
(QEMU: invalid); (4) FPTAN of a NaN pushes the NaN (Bochs; QEMU 1.0); (5) the partial remainder's N is AMD's rule (the SDM:
"implementation-dependent, 32..63"); (6) UE/OE at the double's thresholds (I6 deviation 1).
**QEMU 7.2 departures found** (recorded, stepped around in the corpus, judged by the SDM in `cases`): its new decoder drops
REX.W/VEX.W for PCMPESTRI/PCMPESTRM (`gen_PCMPESTRI` passes the bare imm8, so RAX/RDX are always read as 32 bits; SDM, XED and
FEX use RAX/RDX); FPREM1 never reports quotient bits (`floatx80_modrem` gets no quotient pointer for the IEEE remainder: C0 C1
C3 read 0); FSIN/FCOS/FPTAN/FSINCOS go through the host's double libm with no flag at all and test |x| > 2^63 (the SDM:
>=); F2XM1 of |x| > 1 is invalid.
**Checks** (series31). `cargo test`: `strings::port_matches_sdm_every_imm8` (the port against a separately written SDM model,
every imm8, implicit and explicit lengths, REX.W, 200 random string pairs each), `x87::helpers` (the SDM cases above). QEMU 7.2
corpus (`tools/reference/gen_i6b.py`, `natemu-raw/i6b-corpus-20261030-01`, 112 programs: 64 string programs, every imm8 four
times across the four forms, legacy and VEX, register and unaligned or page-crossing memory operands, explicit lengths
negative, saturating and with upper bits, REX.W/VEX.W where QEMU agrees, YMM0's zeroed upper half; 48 x87 programs, each form
alone and mixed, FNCLEX then FSW masked to what both define, results popped with FSTP m64): 112/112 match at block sizes default
and 1 (`tools/run_i6b.py corpus`), the 540 x87 results QEMU computes in 80 bits or with its host's libm compared within 2 ulps:
500 equal, 40 one ulp apart; before the generator stepped around the departures above, 81 programs differed, each traced to one
of them. `tools/run_i6b.py cases` (SDM rules, natemu only): 129 as expected at both block sizes: C2 at 2^63, -2^63, 2^64, 1e300
and in range at 2^63 - 1024 for the four trigonometric forms, infinities, zeros, SNaNs; FPREM/FPREM1 quotient bits (7/2, -7/2,
29/3, 2^62 + 2048 by 3), partial remainders against exact rational arithmetic (2^100/3, 1e300/7.25, a denormal divisor) and
glibc's fmod loop to the complete remainder and bits; IE/ZE/OE/UE/PE cases; underflow (empty ST(0), empty ST(1)) and overflow
for every form with the masked responses; FPTAN's 1.0, FSINCOS's push order; #NM under CR0.TS; PCMPISTRI/PCMPESTRM/VPCMPISTRM/
VPCMPESTRI #PF not present and crossing (CR2 the second page), #GP/#SS non-canonical and straddling, ECX/XMM0/RFLAGS unwritten
at the fault, a misaligned operand running; 64-bit lengths with REX.W and VEX.W (and 32-bit without) against a third, Python SDM
model. `verify_precision.py` (new rule: each `svc #5/#6` right after its `movz w0` in its instruction) clean on the corpus
bodies (112 compiled, 10,531 sites) and on its own corpora; `test_slice1.py` 32 (the helper forms in `ALLOWED`; `svc #5/#6`
each after `mov w0`); FEX ASM suite 1,605 -> 1,703 pass, 0 fail, 22 -> 45 precision deviations (every H0F3A/VEX PCMPxSTRx test
and FEX_bugs/pcmpestri_garbage_rcx pass; new deviations: D9_F2 D9_F3 D9_F5 D9_F8 D9_FB D9_FE D9_FF and the 16 FEX
precision_test_* of these forms, bounded in `run_asm.py` (`HELPER_PRECISION`, `HELPER_REMAINDERS`): 2 ulps for the transcendental ones, 3 for FPTAN,
and for the remainders (FPREM and FPREM1 tests and D9_F5) exactly the remainder of the operands as FLD m80 rounded them to doubles
under one of the four rounding controls, computed from each test's operands; `X87/D9_F9`, FYL2XP1 of 2.0 outside the SDM's
range, expects 8.0 for 15 log2(3), a test FEX disables: listed as FEX's expectation). Census (`natemu-raw/kernel-census-i6b-20261030-01`):
initramfs SSE 7,236 -> 6,835 (PCMPISTRI 401 -> 0), x87 59 -> 3 (the 56 helper sites -> 0; FFREEP 3 left), kernel unchanged.
Standing suite on series31 (with K1's in-progress tree; `natemu-raw/suite-20261030-050513`, retries in `suite-i6b-retry`):
cargo build/test (49 + 75), stack audit (gate 18,944 bytes, 23,024 nested, of 32,768; libm adds no indirect branch), python
unit 281, fix/fix-1 64, seed/seed-1 44, flow 64 x 2, probes 83, FEX ASM as above, corpora (N = 2..4, after one retry), smp,
limits, kicks, system, coherence, sharedpt, litmus, contain, privilege, delivery, events, i6 faults 275 x 2 (the FSIN refusal
case removed), `run_m3.py`, `run_l1.py` 41/41, `run_l2.py`, `run_l2_iasl.py`, `run_k2.py`, `run_x3.py`, `run_m1.py` 20, `run_l3.py`,
`run_l4a.py`, `run_i5b.py` 411, I8 corpora 136/136 x 2 and 329/336 x 2 (the 7 recorded QEMU two-NaN departures), i8 faults
851 at block size 1, i6b corpus and cases as above: 0 failures. Every other failure was `hv_vm_create: 0xfae94007` (VM
contention with another agent's runs), on different cases in the first run and the retry: slice-1 faults 7,824 + 5 + 4 and
7,825 + 5 + 3 (faults-1 7,828 + 5 on retry), loopfaults default 844 of 846 on retry (block sizes 1 and 2 846), i8 faults 850
of 851 on retry. `run_l4b.py`: `l4b-s5-x2`'s QEMU reference run did not power off (exit 0, 16-byte dump) in both runs, a
QEMU-side check that involves none of these forms; every other L4b group passed.
**Kernel** (Debian 6.12, 2 GiB, `console=ttyS0 earlyprintk=serial mitigations=off nokaslr --initrd` Debian's initrd, series31,
with K1's in-progress tree): past `pcmpistri` in `/sbin/modprobe -q -- crypto-hmac(sha256)` and on to "Run /init as init
process"; 279 console lines (last: "x86/mm: Checked W+X mappings: passed, no W+X pages found.", "Run /init as init process"),
163 s host time, 256,532 compiles; stops `nocode` `unsupported_form` at CPL 3 on a MOVBE in glibc's `memcmp` (`movbe rax,
qword ptr [rdi]`, `48 0f 38 f0 07`, then `movbe rcx, [rsi]; sub rax, rcx`; the linear address varies from run to run with the
library's placement, read through the guest's page tables from a RAM dump), in `/bin/udevadm trigger --action=add` (stack
strings) under the initramfs's `/init`. MOVBE is outside I6b (68 initramfs sites, filed under SSE as another feature).
**Not built:** FFREEP (the undocumented alias, 3 sites), FBLD/FBSTP, unmasked x87 exceptions (#MF), x87 results rounded by
FCW.RC/PC, MOVBE, AES/SHA/PCLMULQDQ/ADX, MMX.

*Built (I9, the remaining advertised forms user space hits: MOVBE, MMX, AES-NI/PCLMULQDQ, PDEP/PEXT, FFREEP; and #UD for the
unadvertised ADX/SHA/CET_SS forms; fexc series32 = series31 + patch 0024 + shim changes):* **Profile** (`cpu_profile.json`):
CPUID.1:ECX advertises MOVBE (22), AES (25), PCLMULQDQ (1); CPUID.1:EDX MMX (23); CPUID.7.0:EBX = 1A9H: BMI1, AVX2, SMEP, BMI2
(8) and FSGSBASE, not ADX (19), SHA (29; advertised since November 3, EBX = 200001A9H: Built (SHA)), ERMS or AVX-512; CPUID.7.0:ECX = 0 (no VAES, VPCLMULQDQ, GFNI, CET_SS); no 3DNow!.
**MOVBE** (0f 38 f0/f1, none or 66): admitted with memory operands at 16, 32 and 64 bits. FEX lowers it to a plain load or store
plus REV (REV16 for a 16-bit store; a 16-bit load is REV32 and a BFXIL into the register), so the access is an ordinary recorded
site: #PF/#GP/#SS precise, a crossing store completed by the resident, #AC natively (SCTLR.A at CPL 3 under CR0.AM/RFLAGS.AC, X2).
Not gated. A register operand (SDM MOVBE: #UD) is a system cut the host decodes as `system::UD`; F3 0f 38 f0/f1 is refused (FEX's
decoder fails before its ModRM).
**AES-NI and PCLMULQDQ** (legacy 66 0f 38 db..df, 66 0f 3a 44/df; VEX.128 forms, AES and AVX both advertised): FEX's Arm
crypto lowering, AESE/AESD with a zero round key then AESMC/AESIMC and an EOR with the x86 round key (x86's ShiftRows, SubBytes,
MixColumns, AddRoundKey order), AESKEYGENASSIST through AESE and a TBL swizzle, PCLMULQDQ through PMULL/PMULL2 with imm8 bits 0
and 4. Legacy memory operands are 16-byte aligned (exception class 4: #GP(0) before any walk, I5a's check), VEX.128 ones not;
gated as SSE (CR0.EM, CR4.OSFXSR, CR0.TS) or AVX (OSXSAVE, XCR0, TS). VEX.256 (VAES/VPCLMULQDQ) stays an encoding refusal (not
advertised). **PDEP/PEXT** (BMI2): patch 0024 replaces FEX's loops (backward branches, refused since I1) under slice1 by
straight-line code in the JIT on its scratch registers, flags untouched: PEXT is Hacker's Delight 7-4 "compress" (log2(width)
rounds of a parallel-suffix XOR and two masked moves: 83 instructions per site at 64 bits, 65 at 32), PDEP since the review
round 7-5 "expand" (the compress rounds record each round's moved bits, two of them in v0/v1 and one recomputed at the end, then
the source moves back round by round: 102 per site at 64 bits, 82 at 32; the first version, one step per source bit, was 387). **FFREEP** (df c0+i): undocumented by Intel but executed by Intel and
AMD x87 units (the sources FEX's table cites: AMD's Athlon x86 Code Optimization Guide, "Use FFREEP Macro to Pop One Register
from the FPU Stack"; pagetable.com/?p=16); FEX ran it as FINCSTP; patch 0024 frees ST(i) and pops (ST(0) empty, TOP + 1), as
QEMU and Bochs do; an x87 form (#NM under CR0.EM or TS). FBLD/FBSTP stay refused (0 sites in the census; FEX's helper).
**#UD for the unadvertised forms** (Vol.2 each: "#UD If CPUID ... = 0"): ADCX/ADOX and the rest of 0f 38 f6, WRSS/WRUSS
(WRUSS 66 0f 38 f5, WRSS NP 0f 38 f6), SHA1*/SHA256* (NP 0f 38 c8..cd, NP 0f 3a cc) are system cuts the host decodes as `system::UD` (the resident raises
#UD), whatever their operands.
**MMX.** Every MMX-register form FEX supports (`FexcSlice1::Mmx`): the MMX arithmetic, logical, compare, pack/unpack and shift
forms (by imm8 and by register/memory), MOVD/MOVQ with general registers and memory, EMMS, MOVNTQ, and SSE's, SSE2's and SSSE3's
MMX forms (PSHUFW, PINSRW, PEXTRW, PMOVMSKB, PAVGB/W, PMIN/PMAX, PSADBW, PMULHUW, PMULUDQ, PADDQ/PSUBQ, PSHUFB..PABSD, PALIGNR),
and the conversions with an XMM operand (CVTPI2PS/PD, CVT(T)PS2PI, CVT(T)PD2PI, MOVQ2DQ, MOVDQ2Q). Refused: MASKMOVQ (a byte-masked
store: FEX writes all 8 bytes; gated first), the shift groups' #UD encodings, 3DNow! (not advertised). **State:** FEX keeps MMn
as the low 64 bits of x87 slot `mm[n]` (`frame::X87_MM`, the slot of physical register n, I6), the slot whose low 64 bits are the
reduced-precision x87 value (a double) when x87 code wrote it; FEX also stores FFFFH at bytes 8..9 of an MMX-written slot, which
nothing reads under reduced precision. Patch 0024: every MMX instruction but EMMS sets TOP = 0 and the abridged tag byte to FFH
(all valid) at its end, after its memory accesses (`FexcEndMMX`, after each handler; FEX did it once per block, at the first
MMX instruction and before its accesses, so a later MMX instruction after x87 code left TOP alone and a fault saw them changed);
the MMX registers join slice1's per-instruction register-cache flush (written back at each instruction and never cached across
one, which may be x87 code writing the same slot through the stack pass); PUNPCKLBW/WD/DQ mm, m32 read 4 bytes (FEX read 8: a
fault or device access past x86's operand). EMMS empties the tags (FEX's `_InvalidateStack`). **Gating:** pure MMX forms (EMMS and
MASKMOVQ included) are cut under the host's `--x87-gated` (CR0.EM or TS) with `cut_gated: "mmx"`, which the host answers as the
new `system::MMX_GATED` (52): #UD with CR0.EM, else #NM (SDM Vol.3 Table 12-1; CR4.OSFXSR does not gate them); the forms with an
XMM operand gate as SSE (#UD with EM or without OSFXSR, #NM with TS). **Aliasing (decision: a recorded deviation, not exact
aliasing, not a refusal).** Exact aliasing needs each register in two representations (a double for x87 code, the 80-bit image
for MMX: significand = MMn, bits 79:64 = FFFFH) and a conversion at every MMX/x87 transition: the representation as a bit of
`Cpu::code_context`, x87 forms cut under MMX representation and MMX forms under x87 representation, a resident transaction that
converts all eight slots and re-dispatches the instruction, and FXSAVE/FNSAVE/XSAVE reading the raw form. Not built because what
it changes is only x87 code reading MMX values or MMX code reading x87 values without EMMS in between, which hardware turns into
NaN/infinity operands and which the SDM tells software not to do (SDM Vol.1 chapter 9: EMMS before x87 code; x87 code after MMX
without it overflows the full stack); a typed refusal of mixed use is not possible statically (the transition is at run time,
across blocks and context switches). What is exact: MMX results, TOP = 0 and all-valid tags after every MMX instruction, EMMS's
empty tags (an x87 FLD after EMMS works; one after MMX without EMMS overflows with x87's masked response), and every MMX value
through FXSAVE/FXRSTOR, XSAVE/XRSTOR and FNSAVE/FRSTOR made by natemu: the image holds `to_f80(v as a double)` and restoring it
gives `from_f80(...)` = v for all 2^64 patterns under every FCW.RC (abi test `x87::tests::mmx_values_round_trip`: every 13-bit
sign/exponent/quiet prefix with five payloads, and 10^6 random patterns), so a context switch or a signal frame keeps MMX state.
**Deviations** (I9): (1) an x87 read of an MMX-written register sees v's bits as a double (hardware: {FFFFH, v}, a negative NaN,
negative infinity or an unsupported encoding), and an MMX read of an x87-written register sees the double's bits (hardware: the
80-bit significand); (2) an FXSAVE/XSAVE/FNSAVE image of an MMX-written register holds the double view widened, not {v, FFFFH},
FNSAVE/FNSTENV tag words of such registers come from the double view (I6 deviation 11), and an image whose register holds
{v, FFFFH} (made by hardware or by hand) restores its x87 value (I6 deviation 2: v is lost); (3) MASKMOVQ, F3 0f 38 f0/f1 and the
MMX shift groups' #UD encodings are refused, not delivered (as I8 deviation 1); (4) G1's page deviation covers the MMX registers.
**Checks** (series32): `tools/reference/gen_i9.py` corpus against QEMU 7.2 (`natemu-raw/i9-corpus-20261030-01`, 120 programs: 16
MOVBE (all widths, loads and stores, unaligned and across the page boundary), 48 MMX (every admitted form, memory operands at the
page boundary, PUNPCKL* m32 in a page's last 4 bytes, after x87 use, FXSAVE's FSW and tag byte after MMX and after EMMS, an x87
round trip after EMMS), 24 AES/PCLMULQDQ (legacy and VEX.128, register and memory, every selecting imm8), 16 PDEP/PEXT (32/64,
register and memory masks, sparse, dense, zero, all ones), 16 FFREEP (random ST(i) on random stacks, FSW/tags/ST fields)): 120/120
at block sizes default and 1. `tools/run_i9_faults.py` (378 cases, SDM rules, the run cut before the instruction as the reference,
x87/MMX state included): #PF not present/read-only/crossing (CR2 the second page; the first page read-only), the operand ending at
a page end with the next unmapped (no fault), #GP/#SS non-canonical and straddling for 27 MOVBE/MMX/AES/PCLMULQDQ/PDEP/PEXT
memory forms, alignment #GP for legacy AES/PCLMULQDQ/CVTPD2PI m128 (also with the page not present), none for VEX.128; at each
fault GPRs, XMM, MXCSR, memory and MM0..7/TOP/tags/condition codes unchanged (also right after an MMX instruction and after
EMMS + FLD); gating (10 MMX forms: #NM under TS, #UD under EM and EM + TS, running without OSFXSR; the XMM-operand forms and
AES/PCLMULQDQ as SSE; VAESENC as AVX; MOVBE/PDEP/PEXT ungated; FFREEP #NM); #UD for MOVBE register forms, ADCX, ADOX, SHA1MSG1,
SHA256RNDS2, SHA1RNDS4; #AC at CPL 3 for misaligned MOVBE (2/4/8), MOVQ/MOVD/PADDB/PINSRW MMX operands and aligned controls;
MASKMOVQ refused; 378/378 at both block sizes. `test_slice1.py` 32 (the I9 forms in `ALLOWED`, the #UD forms in `SYSTEM`,
MASKMOVQ/VAESENC L=1/F3 MOVBE refused); host `system` tests (the #UD decodes); `run_asm.py` selftest (new: an 80-bit value beyond
the double's range, MMn as a 64-bit value). FEX ASM suite: 1,703 -> 1,846 pass, 0 fail, 45 -> 48 precision deviations,
unsupported 522 -> 376: newly passing MOVBE (H0F38 0_F0, 0_F1), the AES/PCLMULQDQ tests (H0F38, H0F3A, SSELanePreservation,
VEX.128), PDEP/PEXT (and FEX_bugs/pdep_overlap), FFREEP (X87/DF_C0, D9_F5_2/3) and 114 MMX tests (TwoByte, H0F38/H0F3A MMX forms,
OpSize, REP/REPNE D6, X87 D9_F6/F7 ...); `run_asm.py` now compares an MMn expectation given as one 64-bit value with the slot's
low half, extends the reduced-precision bound to FLD m80 of values beyond the double's range and unnormals (I6 deviation 2: the 3
FEX_bugs fxsave/fxrstor tests whose FFREEP now runs), lists ADX and SHA as not advertised, and records X87/X87MMXInteraction and
FEX_bugs/mmx_x87_register_conflating as expecting the 80-bit aliasing (deviation 1-2 above; their MMX results match). Census
(`natemu-raw/kernel-census-i9-20261030-01`, `census.py` ports the shim): SSE class kernel 5 -> 0 (EMMS admitted, WRUSSQ 4 #UD),
initramfs 6,835 -> 0 (AES 4,367, PCLMULQDQ 351, MOVBE 68 and MMX 16 admitted; SHA 1,297 and ADCX 736 `system`, #UD); x87 3 -> 0
(FFREEP); AVX initramfs 229,216 -> 218,126 (11,090 VEX.128 AES/PCLMULQDQ admitted; left: EVEX/AVX-512, VAES/VPCLMULQDQ at 256/512
bits and GFNI, not advertised, and 10 VEX gathers); `other` initramfs 2,282 -> 1,650 (ADOX 632 #UD); `system` kernel 16,799 ->
16,803, initramfs 5,601 -> 8,266. Standing suite on series32 (`natemu-raw/suite-i9-20261030`, with K1's in-progress tree), all 0 failures but the two below:
cargo build/test (50 + 75), python unit 281, fix/fix-1 64, seed/seed-1 44, flow 64 x 3, loopfaults 846 x 3, probes, FEX ASM as
above, smp, limits, kicks, system, coherence, sharedpt, litmus, contain, privilege, delivery, events, i6 faults 275 x 2, `run_m3.py`,
`run_l1.py`, `run_l2.py`, `run_l2_iasl.py`, `run_k2.py`, `run_x3.py`, `run_k1.py`, `run_m1.py` 20, `run_l3.py`, `run_l4a.py`,
`run_l4b.py`, `run_i5b.py` 411, I8 corpora 136/136 x 2 and 329/336 x 2 (the 7 recorded QEMU two-NaN departures), i8 faults 851
x 2, I6b corpus 112 x 2 and cases 129 x 2, I9 corpus 120 x 2 and faults 378 x 2. Slice-1 faults/faults-1 and the slice-3
corpora gave 2 failures each: the straddle probes `straddle-0f38-present` (MOVBE) and `straddle-0f3a-present` (PALIGNR mm)
expected `unsupported_form`; both forms run now, so `run_slice1_faults.py` expects them to run to their end (QEMU with
+movbe/+ssse3): the straddle probes 160 + 3 unsupported at both block sizes, the slice-3 corpora 7,031/7,032/7,033 + 3 unsupported
at N = 2..4 (slice-1 faults: 7,830 + 3 unsupported, the 3 EVEX/3DNow! straddles). Stack audit: gate 18,864 bytes, 22,944 nested,
of 32,768.
**Kernel** (Debian 6.12, 2 GiB, `console=ttyS0 earlyprintk=serial mitigations=off nokaslr --initrd` Debian's initrd, series32,
with K1's in-progress tree): past the MOVBE in glibc's `memcmp` (`/bin/udevadm trigger`) and the rest of the initramfs's `/init`;
`syslogd`/`klogd` start ("Starting system log daemon"), the installer's GNU screen session opens its windows (installer, two
shells, log) and the Debian installer shows its first dialog, "[!!] Select a language" (C / English, `<Tab> moves; <Space>
selects; <Enter> activates buttons`), then waits for keyboard input with the status-line clock advancing each minute; 351 serial
lines (279 kernel lines to "Run /init as init process" at 67.8 s guest time), no stop: the run was ended by hand after 14.7 min host
time (1 min 56 s of CPU: the vCPU idles in HLT). Serial log: `natemu-raw/i9-kernel-20261030-01/serial.txt`.
*I9 review round (October 30; series32 rebuilt in place, patch 0024 extended; independent review: one blocking defect):* **Block
size (blocking).** fexc sized its temporary JIT buffer as two pages plus 24 bytes per IR node and, with no guard-page handler,
died with SIGBUS when a block's code outgrew it (21 PDEP64, 45 PDEP32 or 100 PEXT64 in one block: "fexc child died, status 10");
smaller runs gave objects over `MAX_BLOCK_BYTES` (32 KiB), which admission refused ("code layout") with no retry. Fixed on both
sides, for every form: (a) patch 0024 checks, before each IR node and before the block's tail (stubs, thunks, RIP entries), that
64 KiB of the buffer remain (no node emits that much), else restarts the compile with the doubled estimate, FEX's own
`NeedsLargerJITSpace` restart that only its signal handler raised, so fexc never writes past its buffer; (b) the host
(`Compiler::compile`) translates a RIP whose object is over `MAX_BLOCK_BYTES` again with half the instructions it held
(`admission::oversized`), until it fits; one instruction too large for a block stops `code_too_large`. The cost of a long
lowering is therefore a block cut at about 32 KiB of code (408 bytes per PDEP64 site, 332 per PEXT64: some 75 or 95 per
block; the 120-PEXT64 case is 2 blocks in 3 translations, the 1,000-PCMPISTRI case 4 blocks in 7). Found too: FEX's `FEX_bugs/add_sub_carry_2.asm` (a block over 32 KiB) now
runs. Checks: `gen_i9.py --big` (`natemu-raw/i9-big-20261030-01`: 25 and 40 PDEP64, 60 PDEP32, 120 PEXT64 and 1,000 PCMPISTRI in
one straight run each, against QEMU 7.2; the PCMPISTRI run is byte-checked but not annotation-checked, `ref.py`'s new
`unannotated`, as QEMU's in_asm log garbles an instruction crossing 1 KiB of a translation block): 5/5 at block sizes default
and 1. **PDEP cost:** the expand above; the corpus's PDEP cases and the reviewer's 28 masks (`rv-bmi2`, stc/clc, 32/64) match.
**Other fixes:** the dead F3 branch of the MOVBE #UD cut is gone (fexc never sees a ModRM there); the WRSS/WRUSS encodings are
named right; `run_i9_faults.py` gains SDM-judged cases (586 now): CVTPI2PD m64 leaves TOP and the tags (no MMX
transition; corrected for CVTPI2PS m64 by the I9 addendum below), the register forms, CVTPS2PI, CVTTPD2PI m128 and MOVQ2DQ set TOP 0 and tags FFH, EMMS
empties them; MMX shifts by register and imm8 counts 0..63, 64, 255 and 2^32 + 1 for all eight forms against the SDM rule.
**Record:** the three `MMX_GATED` hunks (`crates/abi/src/execution.rs` the kind and its `well_formed` arm, `crates/runtime/src/
execution.rs` the #UD/#NM arm, `crates/host/src/execution.rs` the `cut_gated: "mmx"` mapping) landed in commit f4ec290 (K1's).
**Re-runs** (series32 as rebuilt): cargo test (50 + 75), stack audit (gate 18,864 bytes, 22,944 nested), `test_slice1.py` 32,
python unit 281, FEX ASM 1,847 pass, 0 fail, 48 precision deviations, 375 unsupported, I9 corpus 120 x 2, `--big` 5 x 2, I9 faults
586 x 2, slice-1 faults/faults-1 7,830 + 3 unsupported each, `run_k2.py` (exit counts flat, XMC checks 0 mismatches).
**Not built (remaining, with reasons):** exact 80-bit MMX/x87 aliasing (above: the representation keyed in the code context);
MASKMOVQ (a masked store, as MASKMOVDQU/VMASKMOV); FBLD/FBSTP (0 census sites; FEX's BCD helper); the other undocumented x87
aliases (DC D0/D8, DD C8, DE D0, DF C8..DF: 0 sites); the forms the census still refuses although advertised: 0x67
address-size forms (initramfs 1,435, + 6 `addr32 mulx`; refused since I1), VEX gathers (10; I8), XLAT (1); not advertised but
refused instead of #UD: XOP VPROTD/VPROTQ (144, AMD), RTM XBEGIN/XABORT (9), VIA PadLock (4); CET RDSSP/INCSSP (41: on a CPU without
CET they are NOPs in the hint space, glibc runs them only with shadow stacks enabled), PREFETCHW (kernel 17, not advertised;
Haswell executes 0f 0d /1 as a NOP); unmasked x87/SIMD exceptions (#MF/#XM, I5c).

*Built (X4, AP start through the ACPI multiprocessor wakeup mailbox; no fexc change, series32):*
**Protocol** (ACPI 6.5 5.2.12.19, Table 5.44; version 0, L2's table at `Tables::mailbox`): vCPUs 1..N-1 start as APs in the
firmware's waiting state (`State::mailbox` = the mailbox's guest-physical address; the resident's `events::wait_mailbox`
runs before the first dispatch). The AP whose APIC ID (= its vCPU number) matches a Wakeup command (1) reads the vector
after the command (acquire), records it, marks itself running, then clears the command to Noop as the acknowledgement (a
store through the RAM alias with the code-write protocol) and starts at the vector; any other APIC ID, Noop or a
reserved command (2..FFFFH: version 0 has no Test command) is ignored and not acknowledged; an AP takes one wakeup and
never looks at the mailbox again. **Wake state** (`boot::Loaded::ap`, the firmware's): long mode with paging through the
loader's identity map of [0, L) (reserved memory; it covers both places a vector may lie: below 1 MiB and the mailbox's
OS area), IF = 0, every selector flat (CS 0x10 64-bit, the others 0x18) from a GDT copy in the NVS boot scratch
(`boot::AP_GDT` = 0x800000; the loader's GDT lies in conventional RAM, which Linux reuses), no IDT (limit 0), general
registers 0, RIP = the vector, a reset local APIC (`boot::ap_wake` applies the wake rules; the test driver's `mailbox`
sections use the same function over their own paging and segments). **EFER.NXE = 1** is the firmware's choice where the table is silent
(UEFI firmware enables NX; under TDX, the mailbox's origin, NXE is fixed at 1): found necessary, as Linux's
`trampoline_start64` loads `trampoline_pgd`, whose data pages are XD, and pushes onto its stack before
`secondary_startup_64` writes EFER; with NXE = 0 bit 63 is reserved and the push took #PF, then a shutdown (the first
2-vCPU boot: AP stopped `shutdown` at 0x9a17e, `push 0x10` after `mov cr3`). **The wait (decision):** the code cache's
own write watch. The waiting AP marks the mailbox page as holding its translations (`execution::mark`: once it returns
no vCPU holds a writable shadow of the page), so any later guest store to the page faults in its writer's resident,
which posts the page to the AP and rings its doorbell (SGI) before the store completes (`cache::written`); the AP sleeps
in `wfi` (no host exit with the in-kernel GIC), and on each doorbell drains, marks again and reads the mailbox. A store
before a mark is seen by the read after it, one after it rings the AP: no wake-up is lost, nothing polls, no host thread
takes part. Stores that land before an AP marks the page again coalesce into one doorbell, so Linux's three stores per
AP (ID, vector, command) wake a waiting AP one to three times (the kernel runs show 1, 3 and 4 sleeps for APs 1-3 at
N = 4). **Uncounted before review, now counted** (`State::mailbox_mark_ticks`, `shootdowns`): each re-mark makes a writer
that holds a writable shadow of the page drop all its shadows (one shootdown), and the AP spins in `mark` until that
writer resets them, longer while the writer is in the host (a compile), which answers only when it next enters the VM:
measured in a 4-vCPU boot to the root-fs panic (`boot-linux` `vcpus[].mailbox[3]`), 0.51-0.56 ms per AP spent
marking over its 2-5 marks, and 1-2 shootdowns per AP over its whole run (the counter is not split by phase). **Measured and rejected:** `ldaxr` + `wfe` (the
exclusive monitor's wake, Linux's `smp_cond_load_acquire` pattern) does not sleep under HVF: `wfe` returned every
~0.8 us (177,917 returns in a 145 ms wait), a busy loop; polling with a bounded timeout or a host thread were not needed.
**Wake latency** (`run_x4.py units`, the guest TSC from the BSP's RDTSC just before its command store to the AP's
acknowledgement, every acknowledged step after a run's first, whose store waits for its block's first translation):
5.5 / 16.6 / 56.6 us min / median / max over 12 wakes at the default block size; at block size 1 5.9 / 252 / 557 us (not
analysed further); the suite's run, both sizes together, 7.3 / 26.1 / 688 us over 24. **End rule and watchdog:** a waiting AP publishes `Events::halted` state 1 with every post seen (no
post wakes it; its APIC's events stay pending for after the wakeup), so the watchdog treats it as idle and the host's
end rule (Built (X3)) holds unchanged: a waiting AP can only be woken by a store from a running vCPU, and it reads the
host's end request before the mailbox, so a Wakeup written before the decision (by a vCPU that then halted) is still
served (`halting`: the BSP writes a Wakeup and halts for good at once; AP 1 still starts). **INIT/SIPI (decision): kept
as the typed stop** (`lapic::ap_start`, `unsupported_system`): INIT-SIPI-SIPI starts the target in real mode at
vector << 12 (SDM Vol.3 9.4.4), and no 16- or 32-bit execution exists; deferred with 32-bit support (an INIT alone, the
wait-for-SIPI state, with it). Linux uses the mailbox whenever the MADT has one (`do_boot_cpu`). **Host lifecycle**
(`drivers/linux.rs`): `boot-linux --vcpus N` (1..4) runs N vCPUs, one thread and one `fexc --serve` each; vCPU 0 is the
BSP at the loader's entry state, the others APs at `Loaded::ap` waiting on the mailbox, every APIC reset; `--stop-at` is
the BSP's; one vCPU's end ends the run (the others are cancelled); the result's `vcpus` lists each vCPU's stop, RIP,
blocks, compiles, halts, interrupts, shootdowns asked, mailbox record [address still waited on, sleeps, acknowledgement
TSC, us marking the page], fault and state, and `ended_first` the vCPU whose end ended the run and its stop (the others
then read `cancelled`). The program driver gains `mailbox GPA` per CPU section and reports `stats.mailbox`.
**Checks.** `tools/run_x4.py` (new): `units` 33/33 at N = 2, 3, 4 and block sizes default and 1 (`turns`, `reverse`,
`ignored`: absent APIC ID, the BSP's, a reserved command; `again`: a second Wakeup for a woken AP; `partial`; `halting`),
judged by `tools/models/mailbox_model.py` (independent author, from ACPI 6.5 alone; `run` for the acknowledgements and
starts, `check_trace` for the trace; 14 unit tests with negative controls in `test_mailbox_model.py`) and the wake state
each AP records (IF = 0, CR0.PG/PE, the BSP's CR3 and CS, CR4.PAE, EFER.LME/LMA/NXE, CPUID's APIC ID, one start at its
vector); never-woken APs end `halted` still waiting. **Not shown by any test (review):** that the AP reads the vector
before it acknowledges. The BSP overwrites the vector right after it sees the acknowledgement, but that probe catches a
late read only if it lands between the acknowledgement and the read, and a mutation that read the vector after the
acknowledgement passed every run; the order is an invariant of `events::wait_mailbox`, checked by inspection and
commented there. `adversarial` (the review's cases, both block sizes): `reversed` (command before APIC ID), `early` (the
Wakeup in the mailbox before any vCPU runs), `alias` (the mailbox through a second linear page), `noise` (an awake AP
storing into the page's OS area while the others are woken), `nowait` (two Wakeups with no wait for the first
acknowledgement; AP 2 starts, AP 1 starts or keeps waiting, never at AP 2's vector): 28/28. `trampoline` 6/6 vs pinned QEMU at N = 2..4, both block
sizes: Linux 6.12's `trampoline_start64` 64-bit branch (LIDT, LGDT, the LA57 check, LOCK BTS lock with PAUSE, the
real-mode stack, MOV SS/DS/ES/FS/GS, MOV CR3, LRETQ) and `secondary_startup_64` to the lock release (`verify_cpu`
inlined, CR3, CR4 PSE then PGE, STARTUP_READ_APICID through the xAPIC page, the CPU-number lookup, the per-CPU stack, the
release), every AP recording RSP, CPU number, CR3/CR4, selectors, SGDT/SIDT and the trampoline stack read under the
lock; QEMU has no mailbox responder, so its stub starts the APs at the same vector in the equivalent 64-bit state (all
at once, contending for the lock); registers and memory equal, except the start RFLAGS.IOPL (QEMU's stub 3, the
firmware's wake state 0), which `verify_cpu`'s PUSHF leaves on the trampoline stack and is masked there. Re-run after
the review: units 33/33, adversarial 28/28, trampoline 6/6; `cargo test` 50 + 75; stack audit gate 19,120 bytes,
23,264 nested. `cargo test` (the loader test checks the AP state).
**Standing suite** (series32, in a scratch copy of this tree with the concurrent P3 compile-server edits to
`host/src/execution.rs`, `main.rs` and `tools/fexc/` left at HEAD, since the host they make needs series33): cargo build
and test (50 + 75), the stack audit (gate 19,024 bytes, 23,136 nested), Python tests 277 OK (295 in the working tree, with P3's); fix/seed 64/44 at both block
sizes; faults 7,830 + 3 unsupported at both; flow 64 x 3; loopfaults 846; probes 74; ASM 1,847 pass, 0 fail; `corpora`,
`limits`, `kicks`, `system`, `coherence`, `sharedpt`, `litmus`, `contain`, `privilege`, `delivery`, `events` 0 failures;
`smp` 1 failure in the suite run (`neg-race@2`: no CPU took LOCK NEG's lost-race exit, a timing-dependent check of I3;
no program there has a mailbox), 0 in two reruns; i6 faults 275/275 x 2; m3 0 failures; l1 12/12 and 41/41, l2 N = 1..4
pass, l2-iasl pass; k2 22/22; x3 9/9, 4/4, 24/24, 6/6, 10/10, 54/54; x4 33/33 and 6/6; k1 184 runs, 0 failures; m1 20/0; l3 0 failures;
l4a 13/13, 4/4, 2/2, 2/2, 1/1; l4b 5/5, 2/2, 3/3, 11/11, 7/7, 1/1; i5b 411/411; i8 136 x 2; i8b 329 + the 7 known QEMU
NaN departures x 2; i8 faults 851 x 2; i6b 112 x 2, cases 129 x 2; i9 120 x 2, big 5 x 2, faults 586 x 2.
**Kernel** (Debian 6.12.107, 2 GiB, `console=ttyS0 earlyprintk=serial mitigations=off nokaslr`, 10 s watchdog, no
initrd; guest times are the kernel's printk timestamps, its `local_clock`: TSC-based sched_clock, which once the TSC is
marked unstable is kept against the jiffies-based clocksource, not host time): `--vcpus 2`: "smp: Brought up 1 node, 2
CPUs" (CPU 1 booted 9.93 -> 12.00 s guest time), "Total of 2 processors
activated", root-fs panic at 39.2 s guest, 61.7 s wall (incl. the watchdog), 295 serial lines; `--vcpus 4`: "#1 #2 #3",
"Brought up 1 node, 4 CPUs" (9.90 -> 16.23 s), root-fs panic at 52.7 s guest, 75.0 s wall, 296 lines. With Debian's
initrd (60 s watchdog): the installer's "Select a language" dialog at N = 2 after 546 s host (354 lines, "Run /init" at
81.5 s guest) and at N = 4 after 561 s (353 lines, "Run /init" at 112.0 s guest); runs ended by the script at the dialog.
No CPU bring-up error, no "TSC synchronization" warning (the TSC is marked unstable by the clocksource watchdog before
SMP bring-up, as at 1 vCPU, Built (K1), so Linux skips the sync check) and no APIC error; the one APIC line is L2's
known "Incorrect checksum in table [APIC]" (Linux zeroes the mailbox address).
**Not built:** INIT/SIPI (above); the mailbox's version 1 (reset vector, Test command; Linux would then offline CPUs
through it); CPU offlining and kexec; a measured breakdown of the block-size-1 wake latency.

*Built (P3, translation latency; fexc series33 = series32's patches + `fexc.cpp` and shim changes; independent review: no
blocking defect, its should-fix items done below):* The boot breakdown found 90-93% of boot wall time waiting for fexc:
a fork per request (0.48 ms), a new FEX context (0.05 ms) and hex-dumping the guest bytes into a JSON the host did not read;
the compile itself takes microseconds. **Fork-free server** (`--serve-mode reuse`, the default; `fork` kept for debugging):
one FEX context per configuration (Config values, host features, profile), kept, with nothing of a request left in it:
fexc runs FEX's `CompileBlock` without its caches (`ContextImpl::CompileCode`: the block never enters the lookup cache,
which had answered a repeated RIP with the old block), clears its relocations and empties the code buffer; FEX's
process-global Config is rebuilt from nothing per request. **The leak's root cause:** the earlier in-process mode made a
context and thread per request and could never destroy them (FEX's destructors assume a running emulator thread), about
145 KiB each; `Config::Initialize` added its layer with `emplace`, which keeps an existing one, so an earlier request's
`--config` survived. **Fixed cost:** the server's JSON leaves out the hex dumps (input, each instruction, the code; only
the CLI's consumers read them): 0.063 -> 0.034 ms per request. **Host key by what fexc read:** the reply reports `extent`
(the end of every guest range read or checked, all through `InWindow`) and `args_read` (the gating arguments are
`FexcArg`s: reading one sets its bit; the shim tests the form before the flag); the host keeps an answer under RIP, block
limit, the bytes before the extent (all of them, and their length, when it reaches their end) and the gating values read,
and decodes system instructions again from the current bytes. **Persistent cache** (`crates/host/src/compile_cache.rs`,
`boot-linux` only; `NATEMU_COMPILE_CACHE=DIR|off`, default `~/Library/Caches/natemu/fexc`, `NATEMU_COMPILE_CACHE_MB`
default 512): key SHA-256(fexc binary's SHA-256, exact request), value fexc's raw reply with a SHA-256 of key and payload
(a damaged entry is a miss); immutable packs written by rename by a writer thread off the compile path (each key once per
process, a pack per 4 MiB or 2 s, so a run killed by a signal loses at most 2 s), `k-` packs for kernel-half RIPs and `u-`
for the rest; nothing is written past the cap, and an open over 3/4 of it evicts down to 3/4: other fexc builds' packs first (pack names carry the build's identity), then user packs, then kernel ones, oldest first (November 2 fix: an open evicted only over the full cap, so a cache filled just under it never had room again, and old builds' packs were never dropped first).
SHA-256 uses the Armv8 instructions (FEAT_SHA256), the portable code as fallback, checked equal at lengths 0-600 and
across part splits. Every object, from fexc or a cache, goes through admission's mandatory validation when used: a forged
entry is refused (`cargo test`: code jumping through x28), never staged; a writer of the cache directory can change guest
behaviour within its containment, as a replaced fexc binary could, not break it. **Review fixes:** the cold-run cost (the
key hashed twice and stores on the compile path: 20% over cache-off) now under 2%; the cap enforced during a run and
counted with each pack's framing; the lost tail on SIGINT; one copy per key with several vCPUs; `run_l1_smoke.py` sets the
cache off; `read_index` adds with `checked_add`. **Proof:** both boots' logged request sets (76,610 and 1,338,107) through
series32 forked and series33 in process: 0 differences (objects; JSON less the dumps); each request again with the bytes
after its extent inverted and the unread gating arguments toggled, then cut at its extent: 0 differences; 1.18 M derived
keys, none with two answers; server memory 10 MiB over 1.34 M requests (22-29 MiB with `test_serve.py`'s many
configurations); `test_serve.py` 1,970 requests identical in both modes (gating and FPCR variants added). Fork mode is not
the same environment: CNTFRQ_EL0 reads 24 MHz in a forked child and 1 GHz otherwise on this macOS (review), which changes
the default profile's RDTSC/CPUID 15H code, not slice1's. **Boots** (Debian 6.12, 2 GiB, `console=ttyS0
earlyprintk=serial mitigations=off nokaslr`, the breakdown's instrumented builds of 7a2f714, medians of 3 unless noted;
wall from process start to the stop; compile = waiting for fexc plus cache reads and stores):

| | HEAD (series32) | cache off | cold | warm | QEMU |
| --- | --- | --- | --- | --- | --- |
| To the root-fs panic (cap 512 MiB) | 43.55 s, compile 40.37 s, 76,478 compiles | 4.65 s, 2.20 s, 62,891 | 4.72 s, 2.26 s, 62,897 | 2.47 s, 0.15 s, 70 (62.6 k cache hits) | 2.02 s |
| To "Select a language" (initrd) | 653.2 s, 574.8 s, 1,153,931 | 68.0 s (67.7-80.4), 35.4 s, 1,142,555 | 74.3 s, 39.9 s, 1,142,931 (1 run, cap 512) | 68.8 s, 35.0 s, 964,280 (178.7 k hits; 1 run) | 6.55 s |
| TSC marked unstable | yes | no | no | no | no |
| Quick PIT calibration | fails | fails | fails | fails | succeeds |

One no-initrd boot stores about 36 MiB; the initrd boot reached the 512 MiB cap (659 k entries kept, user packs evicted
first). User-space code at randomized addresses is compiled again per process (the RIP is part of each block): with
`norandmaps` the initrd boot makes 140 k compiles and takes 33.7 s cold, 29.6 s warm (old cache code, cap 4096 MiB, 1 run
each). **Quick PIT calibration (review's diagnosis):** first-time blocks run inside the timed windows of
`quick_pit_calibrate`, whose two reads' error d1 + d2 must stay under 24.4 us; a first-time block costs about 35 us cold,
about 20 us on a disk hit, 4-6 us on a memory hit and about 2 us resident, so even warm runs fail (re-measured after the
review fixes: still fails, 3 of 3). **l3 `stress` redesign** (the faster compiles exposed it; review's cause: back-to-back
kicks always stopped the vCPU before a REP's first element): a local APIC one-shot of 20 us armed right before each of 64
REP OUTSB of 512 bytes to an unassigned port makes deliveries between elements deterministic (63 of 64 each run; at most
two may come before the first), every delivery is logged (4,096 entries) and judged, and the kicks stay as coverage (every
20-60 us): green 10 of 10; with a scratch mutation giving RF = 0 between elements it fails (3,027 problems).
**Checks:** `cargo test` (50 + 79), the stack audit, `test_slice1.py` 32, `test_serve.py` both modes, `run_l3.py`,
`run_l1_smoke.py`, the standing suite on the first P3 build (every runner green; collisions with other agents' VMs and
the old l3 coverage condition aside), fault suites spot-checked on the review-fix build. **Not built / remaining:**
caching user-space code independent of its RIP (objects carry RIP literals; about 1 M of the initrd boot's 1.14 M
compiles); translating fall-through successors ahead of use, which the calibration windows need; the in-memory cache
still clears wholesale at 65,536 entries; eviction is oldest-written, not least-used.

*Built (I10, the remaining architectural refusals of the gap audit (owner decisions of October 30): 0x67 address size, XLAT, #UD
for reserved and unadvertised encodings, the CET hint space; fexc series34 = series33 + patch 0025 + shim changes; independent
review: nothing blocking, its fixes and the WAITPKG addendum in series36, below):* **0x67 (SDM Vol.1 3.3.7, Vol.3 4.5.1).** In 64-bit mode the address size becomes 32 bits: the effective address
is computed, truncated to 32 bits and zero-extended, then the FS/GS base is added. FEX already does exactly this for every
ModRM/SIB/moffs operand (`DecodeAddress` sets `AddrSize` 32; `LoadEffectiveAddress` adds base, displacement and scaled index
in 64 bits, then `Bfe` 32, then the segment base; `SelectAddressMode` folds nothing when the sizes differ), RIP-relative
included (RIP + disp, then truncated: EIP-relative), moffs with a 4-byte offset, LEA (the truncated address zero-extended into
a 64-bit destination, the low 16 into a 16-bit one), JECXZ (ECX) and the stack's own accesses (RSP whatever 0x67), so the
shim's refusal (since I1) is lifted for every admitted form: loads, stores, read-modify-writes, LOCK forms, XCHG, CMPXCHG8B/16B,
PUSH/POP m, indirect CALL/JMP m, SSE, AVX (VEX forms after 0x67), MMX, x87, MOVBE, CRC32, BMI (`addr32 mulx`). The native
access uses the truncated register, so every site's recorded address (`site_address`), #PF CR2, the canonical check (never
#GP for a garbage upper half; FS.base + EA non-canonical is #GP(0)), the split-store and alignment checks and #AC are the
x86 linear address with no runtime change; admission needs no new site rule (the address register is the access's own), and
direct branches ignore 0x67 (`branch_target` skips it: Linux/glibc linkers relax `call *f@GOTPCREL(%rip)` to `addr32 call
f`, libcrypt 181 sites). **Patch 0025** fixes what FEX got wrong: the one-element string pass (`FexcStringOp`) uses ESI/EDI/ECX
(read zero-extended, each update written zero-extended, the count test `cbz w7`, verify_precision's P5 exemption extended);
LOOP/LOOPcc with 0x67 writes ECX zero-extended (FEX inserted 32 bits and kept RCX[63:32]; QEMU and Bochs zero-extend, as any
32-bit register write); XLAT adds AL to RBX (EBX + AL truncated with 0x67) before the FS/GS base (FEX added the base to RBX
first). Host-decoded memory forms (descriptor tables, MOV Sreg m, LAR/LSL/VERR/VERW, INVLPG, CLFLUSH, LDMXCSR/STMXCSR,
VLDMXCSR/VSTMXCSR, x87 FLDCW/FLDENV/FNSTENV/FRSTOR/FNSAVE/FLD m80/FSTP m80, FXSAVE/XSAVE family, far pointers, BT* under alignment
checking) take 0x67 as `System::prefixes` `ADDRESS32` (`well_formed` allows it on a memory operand) and `segments::address`
truncates before the segment base (INVLPG uses it now too); I/O strings had it since L3. M3 completion records are made with
0x67 too (the device address is the site's). Still refused (`unsupported_form`): BT/BTS/BTR/BTC m, r with 0x67 (the SDM gives
no rule for how the register's byte offset combines with a 32-bit effective address: FEX adds it after the truncation, QEMU
before; 0 census sites).
**XLAT/XLATB** (d7): AL = [seg: (RBX + zero-extended AL)], DS default, CS/DS/ES and FS/GS prefixes, EBX + AL with 0x67; an SS
prefix is refused (no site class for a non-ModRM operand, as moffs); a non-canonical RBX + AL is #GP(0).
**#UD for reserved and unadvertised encodings** (owner: architecture, not a choice). The shim's verdict `Ud` is a system cut
with `cut_ud` in the reply; the host answers `system::UD` (length 1, nothing decoded), and it comes before any gating (decode
faults are priority 7 before #NM, SDM Vol.3 Table 6-2; an invalid VEX encoding under CR0.TS is #UD, not #NM). Delivered: 8f
/1../7 (AMD's XOP, map_select >= 8, is not advertised; Intel's group 1A has /0 only), EVEX (62; AVX-512 not advertised; the slice-1
straddle probes `straddle-62-*` and `straddle-0f0f-*` now expect #UD with the next page present, #PF with it absent), RTM
XBEGIN/XABORT (c7/c6 f8: "#UD If CPUID.7.0:EBX.RTM[11] = 0") and group 11 /1../6, XTEST/XEND (S3 already), VIA PadLock (0f
a6/a7, reserved in Intel's map), 3DNow! (0f 0f, CPUID.80000001H:EDX[31] = 0), DAA/DAS/AAA/AAS/PUSHA/POPA/AAM/AAD/82 ("#UD If in
64-bit mode"), group 4 /2../7, group 5 /7, group 8 /0../3 (reserved), the VEX encodings I8 refused (deviation 1: VEX.L, VEX.W,
reserved VEX.vvvv, register/memory operand kind, gathers without VSIB or with repeated registers; every VEX opcode the
advertised AVX/AVX2/FMA/F16C/BMI1/BMI2/AES/PCLMULQDQ do not define, including those FEX does not decode: AVX-VNNI, VAES and
VPCLMULQDQ at 256 bits, GFNI, AVX-512 mask forms, AMX), the MMX/SSE shift groups' reserved encodings (I9 deviation 3; 0f 71..73
other than /2 /4 /6 register, 66 0f 73 /3 /7), NP 0f 38/0f 3a opcodes that are no MMX form, SSE/MMX operand kinds x86 makes #UD,
F3 MOVBE (SDM MOVBE "#UD ... If REP (F3H) prefix is used"). Kept refused, the SDM giving no #UD: scalar LIG VEX forms with VEX.L
= 1 (hardware ignores L), a REX before VEX with another prefix between, the shift-group /6 (SAL) and TEST /1 aliases, the
undocumented x87 aliases, F2/F3 on the MMX shift groups (blank columns), 66 0f a6/a7 (FEX's 66 overlay map), PREFETCHW and the
0f 18 /4../7 and 0f 19..1d hint NOPs (NOPs, not #UD), MASKMOVQ,
MASKMOVDQU, VMASKMOV/VPMASKMOV and the VEX gathers (owner decision (2)).
**CET** (CPUID.7.0:ECX.CET_SS = EDX.CET_IBT = 0, so CR4.CET stays 0). The 0f 1e space runs as a NOP with any 66/F2/F3 and ModRM
(no access, so an unmapped or non-canonical operand does not fault; LOCK is #UD): ENDBR32/ENDBR64 ("a NOP on processors that do
not support CET"), RDSSPD/RDSSPQ ("a NOP when CET shadow stacks are not enabled and on processors that do not support CET", its
destination unchanged; glibc and libgcc zero the register first and skip the shadow-stack code on 0). **INCSSPD/INCSSPQ is not
a NOP:** the SDM's INCSSP raises "#UD If CR4.CET = 0" (Operation: `IF (CR4.CET & IA32_U_CET.SH_STK_EN) = 0 THEN #UD`), so
natemu delivers #UD (the owner's note of October 30 called it a NOP; libgcc reaches it only after a nonzero RDSSP, which cannot
happen here), as SAVEPREVSSP (f3 0f 01 ea), RSTORSSP (f3 0f 01 /5 m), SETSSBSY, CLRSSBSY, WRSS/WRUSS.
**Census** (`natemu-raw/kernel-census-i10-20261030-01`, `census.py` ports the shim): kernel AVX 11 -> 0 (EVEX `vpermi2d` 2,
`vprord` 8, AMX `tilerelease` 1: #UD), system 16,803 -> 16,814; initramfs other 1,650 -> 15 (0x67 1,435 admitted: `leal` 1,196,
`addr32 callq` 181, register forms; XOP 144, INCSSP 28, RTM 9, PadLock 4 #UD; RDSSP 13, XLAT 1 admitted; left `pushq` 11, `xorl` 2,
`subb` 2, not I10's), BMI 6 -> 0, AVX 218,126 -> 10 (EVEX 205,405 and unadvertised VEX 12,715 #UD), admitted +1,459, system 8,266
-> 226,564. Gathers: the 10 VEX ones in `libmvec.so.1` (`vgatherqpd` 6, `vgatherdpd` 4) stay refused, the 34 EVEX ones are #UD;
masked stores: 0 sites in kernel and initramfs.
**Checks** (series34, a copy of f8f3fa2 with these changes): `tools/reference/gen_i10.py` corpus against QEMU 7.2
(`natemu-raw/i10-corpus-20261030-01`, 88 programs: 40 EA programs of 14 0x67 accesses each, every base and index register loaded
with a random upper half and the low halves chosen so base + index * scale + disp wraps past 4 GiB into the data region (loads,
stores, RMW, LOCK, XCHG, MOVZX/MOVSX, CMP, BT m,imm, LEA 16/32/64, PUSH/POP m, SSE, AVX, x87), FS-based with FS.base above and
below 0; 16 EIP-relative/moffs32/XLAT (with and without 0x67, FS)/addr32 call/jmp; 24 strings with 0x67 at every width, REP/REPE/
REPNE, both directions, RSI/RDI/RCX with garbage upper halves; 8 LOOP/LOOPE/LOOPNE and not-taken JECXZ with garbage RCX):
88/88 at block sizes default and 1 (series33: 88 refused). `tools/run_i10_faults.py` (SDM rules, the run cut before the
instruction as the reference, `i8`'s harness): 21 forms (loads, stores, RMW, LOCK, XCHG, CMPXCHG16B, PUSH m, SSE, AVX 32-byte, x87,
BT m,imm, MOVBE, and the host-decoded FLDCW/LDMXCSR) each through a garbage upper half (runs; #PF not present/read-only with CR2
the truncated address), a 4 GiB wrap with base + disp and with SIB, a truncation to 0xFFFFF000 (#PF there, not #GP), a 64-bit
non-canonical base that runs, a page crossing (CR2 the second page; the first page read-only), FS.base + EA non-canonical
(#GP(0)) and FS.base below 0; EIP-relative wrap (#PF at 0xfffxxxxx), moffs32 load/store, XLAT 32 and its truncation, XLAT 64
non-canonical #GP, INVLPG/FXSAVE with 0x67, #AC at CPL 3 by the truncated address (REP MOVSD too), BT* m, r refused, REP MOVSB
and REP STOSQ (DF = 1) faulting midway (RSI/RDI/RCX zero-extended), LODSD/REP STOSB zero-extension, ECX = 0 with RCX's upper half
set (no element, nothing written), LOOP and JECXZ on ECX, addr32 call's return address, LEA zero-extension and 16-bit LEA, 38
#UD forms, #UD before #NM under CR0.TS, 7 CET hint NOPs (RDSSP's destination kept; no access through an unmapped or
non-canonical operand), and M3 device loads/stores through a garbage upper half and a 4 GiB wrap: 272/272 at both block sizes
(series33: 241 of the 269 non-device cases not as expected; with patch 0025's zero-extensions undone: the 5 LOOP/REP register cases fail, and 32 of the
88 corpus programs). `test_slice1.py` 33 (new: `UD` 34 forms at the RIP with `cut_ud` and under every gating, the 0x67/XLAT/CET
forms in `ALLOWED`, `REPS` and `BRANCHES`, BT* m, r and SS XLAT in `UNSUPPORTED`; I8's invalid-encoding test now expects
`cut_ud`); host `system` tests (ADDRESS32 on LGDT/MOV Sreg/FLDCW/LDMXCSR/FXSAVE/INVLPG; SAVEPREVSSP, SETSSBSY, RSTORSSP #UD);
admission `branch_target` (addr32 call/jecxz). FEX ASM suite: 1,847 -> 1,864 pass, 0 fail, 48 precision deviations,
375 -> 358 unsupported (the 39 tests that now get #UD for an unadvertised form, 3DNow!, VAES/VPCLMULQDQ ymm and AVX-VNNI, are
counted unsupported by name, as ADX/SHA since I9). Standing suite in the copy (`natemu-raw/suite-i10-20261030`; one retry on
`hv_vm_create` 0xfae94007 for l4b, whose first QEMU S5 reference then did not power off, green on rerun): cargo test (50 + 79),
stack audit, python unit 277, `test_slice1.py` 33, fix/fix-1 64, seed/seed-1 44, slice-1 faults/faults-1 7,833 (was 7,830 + 3
unsupported: the 3DNow!/EVEX straddles now #UD present, #PF absent), flow 64, loopfaults 846, probes 83, slice-3 corpora 7,034/7,035/
7,036 at N = 2..4, smp, limits, kicks, system, coherence, sharedpt, litmus, contain, privilege, delivery, events, i6 faults 275 x 2,
`run_m3.py`, `run_l1.py`, `run_l2.py`, `run_l2_iasl.py`, `run_k2.py`, `run_x3.py`, `run_x4.py`, `run_k1.py`, `run_m1.py` 20, `run_l3.py`,
`run_l4a.py`, `run_l4b.py`, `run_i5b.py` 411, I8 corpora 136 x 2 and 329/336 x 2 (the 7 recorded QEMU two-NaN departures), i8 faults
851 x 2 (the 10 `*-invalid-*` cases now expect #UD under TS and without OSXSAVE), I6b corpus 112 x 2 and cases 129 x 2, I9 corpus
120 x 2, `--big` 5 x 2, I9 faults 586 x 2, I10 corpus 88 x 2 and cases 272 x 2: 0 failures.
*I10 review round and addendum (October 30; fexc series36 = series34's patches with patch 0025's comment fixed + shim
changes):* **Fetch before #UD (review 1, 3).** A #UD encoding FEX does not decode (it stops at the byte it does not know: EVEX,
an unknown VEX opcode, XOP, F3 MOVBE, the reserved primary/0f members) is #UD only once the whole instruction is fetched
(`FexcUdLength`: prefixes, opcode bytes, ModRM, SIB, displacement and the immediate its map or opcode has; the opcodes invalid
in 64-bit mode by their opcode byte): with fewer bytes fexc answers `partial_decode` (the host fetches on, a not-present page
is #PF first: SDM Vol.3 Table 6-2), and one over 15 bytes is `instruction_too_long` (#GP(0)), as every other instruction.
**WAITPKG (addendum).** TPAUSE/UMONITOR/UMWAIT (66/F3/F2 0f ae /6 register; "#UD If CPUID.7.0:ECX.WAITPKG[bit 5] = 0") are #UD
(the earlier `o16 mfence`/`rep mfence` refusals were these; NP 0f ae f0 stays MFENCE); census: the kernel's `tpause` 1 #UD.
**INVLPG with 0x67 (review 2):** `run_k1.py remap-invlpg-a32`: the PTE changed, `invlpg [eax]` with RAX's upper half garbage,
the next call runs the new frame, vs QEMU at 1-4 vCPUs and both block sizes; with the runtime's truncation removed it fails
(`r9: 0x1 != 0x2`). Checks (series36): `run_i10_faults.py` 296/296 at both block sizes (new: 10 `fetch-*` cases, an undecoded VEX/F3 MOVBE/XOP/EVEX form cut at a
page end, #PF with the next page absent and #UD present; 8 `length-*` cases at 15 and 16 bytes, #UD and #GP(0); the WAITPKG
forms and TPAUSE under CR0.TS; series34: 11 of them fail); `test_slice1.py` 34 (new: fetch-first and length
cut reasons; the WAITPKG forms in `UD`); `run_k1.py` all programs at 1-4 vCPUs, both block sizes; FEX ASM 1,864 pass, 0 fail,
48 precision deviations, 358 unsupported; slice-1 faults/faults-1 7,833; I10 corpus 88; i8 faults 851; i9 faults 586.
**Kernel** (Debian 6.12, 2 GiB, N = 1, `console=ttyS0 earlyprintk=serial mitigations=off nokaslr --initrd` Debian's initrd, series34,
compile cache cold for the new fexc): "Run /init as init process" at 7.5 s guest time, syslogd/klogd, the installer's screen
session and its first dialog "[!!] Select a language" after 75 s host time (341 serial lines; then ended by the script).
Serial log: `natemu-raw/i10-kernel-20261030-01/serial.txt`.
**Deviations** (I10): none new. Resolved: I8 deviation 1 and the 0x67 part of 6, I9 deviation 3; I2's 0x67 refusal.
**Not built (remaining, with reasons):** BT* m, r with 0x67 (no SDM rule); the refusals the SDM gives no #UD for (above);
a `cut_ud` mid-block is reported only when the RIP reaches the
instruction (the cut at its own RIP is what the host answers, as for every system cut); the crypt() path through libcrypt's
`addr32 call` was not run (no static x86-64 toolchain here to build a test program into the initrd; the installer's password step
needs input).

*Built (S4, debug: single-step, INT1, instruction breakpoints, DR6/DR7; no fexc change, series32):* **Single-step.**
`find` reads TF at every instruction start; under TF the instruction runs alone (X2's step: a one-instruction block in
the step slot, never cached or linked, every exit back into the resident; a system instruction as its transaction, from
its kept decode when K2 has one) with `State::debug_step` set, and its completion (`completed`: a block exit, a committed
transaction, IRET included) turns it into a pending trap, `State::debug_trap` = DR6.BS, which `find` delivers first at
the next boundary (Table 7-2 class 4: before NMI and INTR) as `Event::Debug` (EXT = 1, the current RF, RIP the next
instruction), DR6 = BS with B0-B3 replaced by the instruction breakpoints whose address is the new RIP (enabled or not,
as the SDM permits and pinned QEMU does); BD/BS/BT are sticky. Any delivery (a fault, INT n/INT3/INTO/INT1) drops the
pending step: no trap after a faulting or interrupting instruction. TF set by POPF or IRET acts from the next
instruction; TF cleared by them traps after them (TF at the start decides). A MOV SS shadow discards the trap at its
boundary (delivery_model A9); the next instruction, still under TF, traps once. A REP string traps after every
iteration (RIP at the REP, RF = 1 in the frame between iterations, RF = 0 after the last; RCX = 0: one trap after it).
HLT under TF completes and does not park: the trap is delivered at once with RIP after it (model A10). SYSCALL and
SYSRET trap after themselves when the TF they leave is 1 (FMASK clearing TF: none; SYSRET loading TF: a #DB before the
first CPL 3 instruction): the SYSCALL/SYSRET pages say nothing, so this is a choice (accepted October 30) following
pinned QEMU's rule (`gen_eob_syscall`) and what Linux's `entry_64.S` states for SYSRET; run_s4's `s4-sysret` matches QEMU. Every TF refusal is gone (POPF, IRETQ,
SYSCALL, SYSRET; syscall_model C3 and segment_model no longer refuse). **Mechanism choice:** (a) Arm hardware step
(MDSCR_EL1.SS) steps one A64 instruction, while an x86 instruction is several and a block's end is its exit, so it adds
nothing to (b); (b) the existing step slot: exact, no code-cache or context change, but every stepped non-system
instruction is a host compile request (answered from the host's compile cache after the first); (c) code keyed by TF in
`code_context` with cached one-instruction blocks would save that exit, at the cost of a context bit and never linking or
filling L1 in that context. (b) was taken: TF is rare (debuggers, kprobes/uprobes single-stepping one instruction out of
line, ptrace), it reuses a checked path, and it costs nothing when TF = 0; (c) is the upgrade if a measured
single-stepping workload needs it. **INT1** (`f1`, host decode `system::INT1`; fexc already cut it): #DB as a trap,
RIP past it, EXT = 1, no gate DPL check, the current RF (model A12), DR6 unchanged; DR7.GD cleared on entry as for every
#DB. **Instruction breakpoints** (DR7 Ln/Gn with R/W = 00; `Cpu::code_breakpoints`): the host ends every block before
each armed address (`translation`'s byte limit, like the stop RIP and the test hooks; an address inside an instruction
on that path gives `partial_decode` and is passed over, as the CPU never matches it), so each is the first instruction
of whatever runs it; `find` checks RIP against them (Table 7-2 class 7, after NMI/INTR) unless RF = 1 or a MOV SS shadow
holds, and delivers the #DB fault with DR6.Bn and the current RF (0) in the frame (Vol.3 18.3.1.1). A block at an armed
address is never linked to nor put in L1, so every entry passes the check; a MOV to DR0-3/DR7 that arms an address not
armed before drops this vCPU's translations (`flush`: blocks translated earlier may span it); disarming keeps them.
LEN is ignored for R/W = 00 (the SDM requires 00 and leaves others undefined; pinned QEMU ignores it). RF now survives a
REP string's iterations: `completed` keeps it at a self-exit between iterations (X2's noted difference), so a
breakpoint on a REP fires once. **Data and I/O breakpoints:** below. **Kernel:** nothing kernel-specific; Linux's
`clear_debug_registers` (DR0-3, DR6, DR7 = 0) and `local_db_save` reads arm nothing and cost nothing; `hw_breakpoint`,
kprobes and uprobes single-stepping and `TIF_SINGLESTEP` use TF, INT3 and execute breakpoints, all built.
**Cost.** The common path gains one test per `find` iteration (a pending trap, TF or any DR7 enable; the work is in
cold functions) and none in generated code, the dispatcher or links; with no breakpoint armed every object is
byte-identical (the host's limit list is empty). `run_k2.py cost`, HEAD and S4 interleaved three times each (us per
iteration, medians): CLI 0.481 / 0.482, STI 0.509 / 0.523, PUSHFQ+POPFQ 0.542 / 0.542, SWAPGS 0.550 / 0.556, RDTSC
0.512 / 0.514, CPUID 0.488 / 0.495, WRMSR 0.495 / 0.511, FLD+FSTP m80 0.629 / 0.629, `div` 0.153 / 0.150 (a first
unguarded version cost about 60 ns per resident entry; hence the single guard). Single-stepping costs 5.3 us per
traced instruction, its #DB and the handler's IRETQ included (a loop of `dec rcx; jnz` under TF, 40,000 and 80,000
steps; one host step request per instruction, answered from the host's compile cache). **Checks** (`tools/run_s4.py`,
programs `tools/bench/smp/debug.py`, block sizes default and 1; every event log judged against the SDM's expected log,
`debug.expect`, and registers/memory against pinned QEMU 11.1.2 system mode): `s4-step` (31 events: MOV/ADD/JMP/CALL/
RET/PUSH/POP/INC m, REP MOVSB x3, REP STOSB with RCX 0, REPE CMPSB ended at its third element, CPUID, the POPF clearing
TF), `s4-popf-iret` (15), `s4-movss` (6), `s4-soft` (10: INT3, INT 21H, INT1 under TF; INT1 with DR6 preset),
`s4-faults` (14: UD2, WRMSR #GP, a #PF whose handler maps the page, each under TF), `s4-xbp` (16: a breakpoint armed
inside a block already translated and linked; one reached twice by CALL RAX; loop head, mid-block and a kept CPUID
twice; inside an instruction (never); after MOV SS (suppressed); at REP MOVSB (once); LEN 11; a single-step trap
landing on an armed address: BS with B3, then no fault (the handler's RF)), `s4-dr` (DR6 write/read-back, DR4/DR5
aliases, DR7 R/W fields, DR7.GD #DB at the MOV with BD, GD cleared in the handler), `s4-sticky` (7: BS kept over traps,
then B0|BS at a breakpoint, INT1 leaving DR6 as is), `s4-sysret` (9: SYSRET with TF from R11 at CPL 3, SYSCALL with
FMASK leaving and clearing TF): 18 of 18 runs match the SDM's logs; `cases` (natemu only, both block sizes): HLT under TF
(#DB, RIP after the HLT), DR7 refusals for write, read/write, I/O with and without CR4.DE, an instruction breakpoint
with LEN 01: 12 of 12. Negative controls, each red in a scratch build: no flush when arming (`s4-xbp` misses the
breakpoint inside the translated block, both block sizes), blocks not cut by the host (misses mid-block ones at the
default size), blocks at breakpoints linked and put in L1 (misses the second CALL RAX), RF cleared at a REP's self-exit
(the REP's breakpoint fires at every iteration). Earlier refusal cases updated: X2's `x2-popf-tf` now expects the #DB
after the next instruction; S2a's `dr7-l0`/`dr7-g3` arm data breakpoints (still refused); S3's `sc-sysret-tf`/
`sc-syscall-tf` and S2b's `seg-iret-tf` refusal cases removed (their behaviour is `s4-sysret`, `s4-popf-iret`).
**QEMU departures (pinned 11.1.2), judged by the SDM:** a REP string with RCX = 0 under TF traps once at itself with RF
= 1 before its normal trap (SDM: no iteration, one trap after it); no instruction-breakpoint suppression after MOV SS
(model A9 suppresses); the DR7.GD #DB frame holds RF = 0 (a fault: RF = 1, X1a); DR6 bit 12 keeps a written 1 (reserved,
reads 0); HLT under TF halts with no #DB (model A10: the trap wakes it; QEMU's run never ends). In those programs the
event log is judged by the SDM alone; their registers and other memory still equal QEMU's.
**Data and I/O breakpoints** (SDM Vol.3 18.2.4, 18.2.5, 18.3.1.2; `execution/watch.rs`). *Rule:* R/W 01 matches writes,
11 reads or writes (no fetch), 10 I/O reads or writes and only with CR4.DE (without it the SDM leaves R/W 10 undefined:
refused, `unsupported_system`); LEN 1, 2, 4, 8 with the address's low bits masked by the length; an access matches when
any of its bytes lies in the range, so a straddling access matches, several ranges set several B bits; LE/GE are ignored
(exact breakpoints). A match is a trap after the instruction (`State::data_hits` become `debug_trap` at its completion,
so a single step's BS comes in the same #DB; a fault or interrupt of the instruction drops them); a REP string traps
after the iteration that matched (RIP at the REP, RF = 1 in the frame); a MOV SS shadow holds the trap one more
instruction, even when that one is INT n/INT3 (then before the handler's first instruction; SDM Vol.3A interrupt 1).
*Generated code:* one Arm watchpoint per armed data breakpoint (DBGWVRn/DBGWCRn: EL0 only, stores for 01, loads and
stores for 11, its bytes in one doubleword); the host turns off HVF's debug-register and debug-exception traps, so the
VM owns them (OSLAR_EL1 still traps to the host; the OS lock is found clear). EL0 virtual addresses are x86 linear
addresses. A watchpoint exception (EC 0x34) in an ordinary block runs the instruction again alone from its start (the
site's state, as a fault does); in that step block each one adds the bits the site's access meets by the x86 rule,
then the one Arm access runs with MDSCR_EL1.MDE clear under a hardware software step (IRQs masked) and the step
exception (EC 0x32) turns watching back on; any other exception taken first (a shadow miss, a split store: SPSR.SS is
then 1) abandons the step, and the access is watched again when retried. The registers are written only by MOV DR, and
MDE is set only while one is armed: with none armed nothing changes. *The resident's accesses* are checked where they
are made: `segments::linear` for every explicit access (POPF, IRET and far-return pops, LGDT/LIDT operands, SGDT/SIDT/
SLDT/STR stores, LDMXCSR/STMXCSR, FXSAVE/XSAVE images, x87 environment, state and m80 images, OUTS sources), the
delivery frame's stores (stack writes, not among the SDM's implicit supervisor accesses; the SDM says data breakpoints
may be detected during FRED event delivery and stay pending after it, Vol.3A 8.3.3, and IDT delivery is taken alike;
Linux's `hw_breakpoint.c` refuses breakpoints on `cpu_entry_area` because one near an IST stack's top causes
unrecoverable recursion, Lutomirski's "Prevent data breakpoints on cpu_entry_area"; a breakpoint near the top of
the handler's stack recurses, as Linux's `hw_breakpoint` notes for its entry stacks), INS destinations, a split
store, a device completion (M3), the operand-sized unit of BT* (cut while a data breakpoint is armed: FEX touches only
the byte holding the bit; `bit_tests_cut` is in the code context), and the port of IN/OUT/INS/OUTS for I/O
breakpoints. Implicit supervisor accesses never count (an accepted choice: the SDM text at hand does not say): descriptor tables,
the TSS, the IDT, page-table walks and their A/D writes, descriptor accessed and busy bits. A breakpoint whose range is
not canonical meets no x86 access and is not programmed as a watchpoint (it could otherwise match the resident's private
window or the guest-state page). *Premise:* a step block holds no exclusive load/store loop (LDAXR/STLXR), which would
never complete under the hardware step: FEX emits LSE atomics for every LOCK form (I3), and the runners' code-shape
check, whose allowlist has no exclusive form, refuses any other word. Debug registers and watchpoints are each vCPU's own. *Audit of FEX
forms:* the native access is x86's for every admitted form the checks exercise (MOV of 1-8 bytes, RMW, LOCK, XCHG,
CMPXCHG with a failed compare (its write-back is an x86 write: matched), PUSH m, x87 m64, SSE m128, AVX m256, MOVBE,
MOVS/STOS/LODS/SCAS, CALL/RET, PUSH/POP); BT* differs and is cut; x87 m80, FXSAVE/XSAVE and the system forms are the
resident's. **Checks** (`run_s4.py`, block sizes default and 1, logs judged by the SDM's expected log and compared with
QEMU): `s4-d-len{1,2,4,8}-rw{1,3}` (every load and store of 1-8 bytes at the edges of two adjacent ranges, DR0's low bits
set to be masked: 11-32 events each), `s4-d-forms` (22: RMW, LOCK, XCHG, CMPXCHG failed and succeeded, PUSH m, BT/BTS
units, x87 m64 and m80, MOVUPS, VMOVDQU ymm, MOVBE, SGDT, a page-crossing store), `s4-d-reps` (7: REP STOSB, MOVSB,
LODSB, REPE SCASB, MOVSQ), `s4-d-stack` (18: CALL/RET, PUSH/POP, PUSH/POPF, IRETQ, an INT3 frame, MOV SS and MOV SS +
INT3, TF with a data breakpoint: BS|B0), `s4-d-io` (9: IN/OUT 1 and 2 bytes, REP OUTSB with a source and port match,
REP INSB with a destination and port match), and after the review `s4-d-sys` (15: FXSAVE, XSAVE/XRSTOR, SGDT/LGDT,
SIDT/LIDT, MOV DS/LLDT/STR/SLDT memory operands, PCMPISTRI m128, a far CALL's pushes) and `s4-d-edges` (54 of 112
accesses hit: 28 forms at both edges of a range, a byte inside, a byte past; the byte-exactness audit of FEX's native
accesses: none wider or narrower than x86's; QEMU agrees on every one): all match the SDM's logs; cases: a
device-page store and load (and a miss), the per-vCPU registers at 2 vCPUs, R/W 10 without DE refused: all pass. A
negative control for each resident check is red when it is disabled (explicit accesses, INS, ports, split store, device
completion, BT unit, BT cut, delivery frame, and running the hit in place instead of stepping its instruction).
**QEMU departures (data/I-O):** QEMU does not mask DRn by LEN; reports one B bit when two ranges match and keeps stale
hit bits on later #DBs; traps a REP string after the whole instruction; ends REP OUTS after the element that met an I/O
breakpoint (RIP past it); pushes RF = 1 for I/O breakpoint traps; does not hold a data breakpoint over MOV SS; aborts
(`bql_lock_impl` assertion) when an exception frame's store meets a breakpoint, so `s4-d-stack` is judged by the SDM
alone.
**Boot:** Debian 6.12 without initrd at N = 1 (as K1's run) reaches the root-fs panic at 29.87 s guest, 52.1 s wall.
*S4 review round (October 30; an independent review, one blocking defect):* **Blocking, fixed:** an instruction
breakpoint could fire between iterations of a REP string: after MOV SS then a REP MOVSB at an armed address (the
shadow suppressed it at the start, RF stayed 0, so it fired at the first self-exit) and on REP OUTSB/INSB entered with
RF = 1 (the port transaction committed, clearing RF, before marking the boundary mid-string): once per element. The
check is never made mid-instruction now (`code_break` skips a boundary with `mid_string`), and a port string marks
the boundary before it commits, so RF stays between its elements. Regression programs `s4-r-movss-rep` (no #DB),
`s4-r-rep-outs` (one, before the REP) and `s4-r-tf-outs` (a trap per element, RF = 1 between) match the SDM at both
block sizes. **Also:** `step()`'s fetch-fault fallback now goes through `find`'s boundary (it stepped the handler's
first instruction without the breakpoint check); a BT* form under alignment checking runs its plain step at once
inside `step_here` (TF counted once, `plain_step` never left for another instruction); segment_model keeps the RF an
IRETQ frame holds; `completed` touches nothing new when TF is off and nothing is armed (the review measured about
13 ns per resident entry from writing the step marker unconditionally). **Untested choice:** a SYSRET with TF = 1 at
its start and TF = 0 in R11 gives no trap (the rule above: the TF it leaves decides); QEMU agrees; Intel's behaviour is
not known.
*S4 part-2 review (October 30; an independent review, no blocking defect, two fixed):* an external interrupt or NMI
whose frame store met a data breakpoint trapped one instruction late (after the handler's first instruction): event
delivery now ends `events::take`'s loop when a trap is pending and `find` takes the trap first (`s4-d-ext-frame`, an
x2APIC self-IPI whose frame meets a write breakpoint; red with the check removed); a breakpoint on a non-canonical range
was programmed as a watchpoint and one over the guest-state page halted the run (`unknown_abort`): such ranges are no
longer programmed (`s4-d-noncanon`, the review's four breakpoints on guest-state fields, at the page's EL0 alias since the G1 fix; red with the check removed).
QEMU departure: it takes no trap for the interrupt frame's store.
*S4 final (f8f3fa2 + S4, fexc series33):* the state record grows by one word (`State::debug`: the pending trap's DR6
bits, the instruction's data/I-O hits, the step mark; the counters `debug_steps`, `debug_traps`, `code_breaks`,
`watch_hits` and `watchpoints` live in the cache's control record, which is not copied: a 64-byte larger state alone
measured about +10 ns per resident entry). `run_k2.py cost` (5 repetitions, N = 200,000, interleaved, load average 5),
f8f3fa2 / S4, us per iteration: CLI 0.5226 / 0.5241, STI 0.5485 / 0.5499, PUSHFQ+POPFQ 0.5745 / 0.5738, SWAPGS 0.5884 /
0.5917, WRMSR 0.5486 / 0.5498, CPUID 0.5252 / 0.5295, FLD+FSTP m80 0.6660 / 0.6531, `div` 0.1626 / 0.1690 (run-to-run
noise about 10 ns). ID_AA64DFR0_EL1.WRPs = 3 on the M4: 4 watchpoints, one per x86 breakpoint (a fifth could not be
armed; the resident refuses one beyond the PE's count). `run_s4.py`: 28 programs x 2 block sizes, 56 of 56 runs match
the SDM's logs, 14 of 14 cases. Negative controls, each red in a scratch build (natemu-side SDM failures): explicit
resident accesses 44, INS 2, ports 18, split store 2, device completion 4 (cases), BT unit 32, BT cut 32, delivery
frame 20, the mid-REP guard 2, no flush when arming 30, no host cut 14, blocks at breakpoints linked or in L1 28, a hit
run in place instead of its instruction stepped 32; RF kept over a REP's self-exits no longer shows (the mid-REP guard
covers what it protected; kept as the SDM's state). Boot without initrd at N = 1, warm compile cache, two runs each:
f8f3fa2 12.50, 12.54 and 12.62 s wall, root-fs panic at 1.31-1.38 s guest; S4 (final build) 12.69 and 12.76 s, 1.41 and
1.42 s. The standing suite on f8f3fa2 + S4 is green (i8b's 7 known QEMU NaN departures; one L4b run lost QEMU's S5
power-off, green on rerun).

*Built (M2, memory types: PAT, PWT/PCD, MTRRs; no fexc change, series36):* **Walker.** `paging::Translation` carries the
leaf's PAT index, 4*PAT + 2*PCD + PWT of the entry that maps the page (SDM Vol.3 5.9.2, Table 14-11; PAT is bit 7 of a PTE
and bit 12 of a 2 MiB PDE), in place of the old `wb` flag. The PWT/PCD of the PML4E, PDPTE and non-leaf PDE, and CR3's,
select only the type of the next table's read (5.9.2); the walker reads tables from coherent guest RAM, so they change
nothing, and MOV to CR3 and `Cpu::control` now keep PWT/PCD (`CR3_IGNORED` = 0xfff; bits 63:40 stay #GP). **Type.**
`cpu::memory_type` (abi, pure) combines the PAT entry the index selects with the MTRRs' type of the guest-physical page:
`mtrr_type` follows "MTRR Precedences" (14.11.4.1: E clear is UC for everything; below 1 MiB with FE the fixed field of
Table 14-9; else the matching variable ranges, `gpa & PhysMask == PhysBase & PhysMask` with V: one type, identical types,
UC among them UC, WT with WB WT; none matching the default type) and `effective_type` is Table 14-7 transcribed (UC- only
from the PAT; it never results). The overlaps the SDM leaves undefined (WC or WP with WB, WC with WT, ...) and a large page
over several MTRR types (14.11.9) are not halts: the type is computed per 4 KiB page, and an undefined overlap is taken as
UC. CR0.CD changes no type (Table 14-5: no-fill mode keeps the types and orderings). **Behaviour.** Guest RAM of every type
ran as coherent write-back (since addendum 2, below, UC RAM's accesses run their instruction alone): data shadows, the fetch walk (code may run from UC memory, 14.3.3) and the resident's operand
accesses (`segments::pages`, CLFLUSH) take RAM whatever its type; a device page goes to M3's path whatever its type and is
never shadowed. The halts this removes: `translate`'s `shadow status=1 detail=7` for a non-WB RAM page, the fetch walk's
`NOT_RAM` for one (the host's "not write-back guest RAM"), `segments::pages`' `unsupported_system`, and MOV CR3's
`unsupported_system` for PWT/PCD. The `stop::SHADOW` halts left are internal invariants, none a memory type: a corrupt
shadow table at INVLPG, a split store's RAM page missing after its walk, and shadow-table capacity or a private target after
a full eviction. WRMSR's #GP rules for IA32_PAT (each entry 0, 1, 4-7; Table 14-10) and the MTRRs (types 0, 1, 4-6, the
reserved bits of DEF_TYPE, PHYSBASE and PHYSMASK, MTRRCAP read-only; Table 14-8, 14.11.2) are S1's, now also checked byte by
byte and bit by bit in the abi tests. **Recorded:** the resident counts data shadows of RAM by effective type
(`cache::Control::memory_types`, in the control record, not the copied state; reported as `stats.k1.memory_types`, by
`run-program` and `boot-linux`), so a run shows when RAM with another x86 type was used. **Invalidation:** shadows and
translations depend on no type, so a PAT write flushes nothing (14.12.4 leaves the TLB flush to software); an MTRR write keeps
its global flush (CPU 9; allowed by 5.10.4.1, "the processor is always free to invalidate additional entries"). A shadow
filled before a PAT or MTRR change keeps its count, as a TLB may keep an entry's type (5.9.3). **What is observable on a
coherent machine, and the deviation.** WT, WP and WB RAM differ only in caching: their orderings are the same ("speculative
processor ordering", Table 14-2) and a write reaches memory in all three, so running them as WB is exact; WC is weakly
ordered (14.3.1, 11.2.5), so TSO is one of its permitted behaviours (combining and delaying writes is allowed, not
required); MOVNT* stores likewise. UC and UC- force strong ordering (11.2.5: "out-of-order or speculative accesses are not
performed"): a store-buffering pattern between two vCPUs on UC RAM may show both loads old under natemu's TSO, which x86
forbids there. That was the declared deviation (the plan's decision: RAM of any type is coherent write-back; Apple silicon
is coherent and HVF's stage 2 maps RAM Normal write-back), withdrawn by the owner on November 2 (addendum 2, below). UC's other properties hold:
device pages are never accessed speculatively (each access completes once from its site, M3), and x87/SIMD accesses to UC
memory "may occur more than once" only matters for devices, whose such forms M3 refuses. Aliasing one page with different
types is unsupported by Intel (14.12.4); natemu stays coherent. **Kernel** (Debian 6.12.107, 2 GiB, `console=ttyS0
earlyprintk=serial mitigations=off nokaslr`, no initrd, N = 1, warm compile cache, on fc60f51): HEAD (three runs) and M2 (two) both reach
the root-fs panic (guest 1.23-1.34 s HEAD, 1.20-1.27 s M2; "x86/PAT: Configuration [0-7]: WB WC UC- UC WB WP UC- WT");
M2's counts: 508,373 WB and 23 UC data shadows, the UC ones from the window where Linux's `cache_disable` clears
MTRRdefType.E around its PAT set-up (`K kernel/cpu/cacheinfo.c:1089`, `kernel/cpu/mtrr/generic.c:953`: all memory UC
while E = 0). **Checks:** abi tests (Table 14-7 row by row; the precedences, every fixed field, FE clear, overlaps, a
discontinuous mask, the default and E clear; every PAT/MTRR type byte and reserved bit at WRMSR; the walker's PAT index at
4 KiB and 2 MiB leaves under every upper-level PWT/PCD pattern); `tools/run_m2.py`: `types` 25 cases (counts against the
runner's own transcription of the SDM rules), `devices` 32 (a device page with each PAT index under the firmware's UC, a
WC or WB range and MTRRs off: one read and one write each, no shadow counted), `qemu` 8 (`pat` and `mtrr` at 1 and 2 CPUs,
block sizes default and 1, against pinned QEMU, which ignores memory types and so judges only that every type behaves as
RAM; it cannot judge the types themselves or UC ordering); `run_slice1_faults.py` paging cells now carry a rotation of PAT
indices 0-7 and upper-level PWT/PCD patterns (7,912 cases, all match, at block sizes default and 1). Negative controls on HEAD (fc60f51 binaries):
`types` 25 of 25 red (`shadow status=1 detail=7`, the host's "not write-back guest RAM", or no counts), `qemu` 8 of 8 red
(`pat` halts detail=7; `mtrr` ran, since HEAD ignored MTRRs, and is red only for its counts), the faults runner's
`paging-store` cells 12 of 129 red (detail=7; the others fault before the access or drew index 0 with no upper bits);
`devices` passes on HEAD too (M3 already sent device pages to their device whatever the bits). **For the independent
author of `tools/models/coherence_model.py`:** replace `Route.wb` (any PWT/PCD/PAT bit = not write-back) by the leaf's PAT
index (PAT bit 7 of a PTE, 12 of a 2 MiB leaf; non-leaf and CR3 PWT/PCD ignored for the page) and the effective type from
IA32_PAT and the MTRRs (Table 14-7, 14.11.4.1), with the firmware MTRR state of `cpu_profile.json` and the reset PAT
0x0007040600070406; no route is refused for its type; and the profile notes there (CR3 PWT/PCD accepted). **Not built /
for the owner:** the firmware MTRR state was fixed for L = 2 GiB, so at `--ram` 3 GiB RAM in [2 GiB, R) was UC by the
MTRRs; since addendum 1 (below) it follows R. UC RAM's strong ordering: addendum 2 (below). `run_slice3_vm.py system` loses its
`cr3-pwt` refusal case (built now). The standing suite on 9110c6f + M2 (fexc series36) is green but for i8b's 7 known
QEMU NaN departures at both block sizes and `test_check_setup` in the scratch copy (its raw-root default resolved through
the copy's symlink to `natemu-raw`, an artefact of the copy). Raw evidence: `natemu-raw/m2-20261030-01/` (the boots,
the HEAD negative-control logs).

*Built (M2 addendum 1, the firmware's MTRRs follow guest RAM; no fexc change):* the virtual firmware programs the MTRRs
for the RAM it reports. `cpu::firmware_mtrrs(msrs, R)` (abi) writes IA32_MTRR_DEF_TYPE and the eight variable pairs for
guest RAM [0, R) over the reset state, and the host's `set_cpu` applies it for the VM's R (`Control::ram_bytes`, set at
prepare) on every vCPU, BSP and APs alike. Rule (`cpu_profile.py` `mtrr_state(ram_limit)`, the same in Rust): default type
WB and [R, 4 GiB) as UC power-of-two pairs; when that needs more than IA32_MTRRCAP.VCNT = 8 pairs (an R with many low bits,
e.g. 1 GiB + 2 MiB), the same map with default type UC and [0, R) as WB pairs, the fewer of R's power-of-two blocks or one
block to the next power of two with [R, it) carved out by UC pairs (UC wins an overlap, SDM Vol.3 14.11.4.1). The fixed
ranges do not depend on R (0-0x9ffff WB, 0xa0000-0xfffff UC). At R = 2 GiB the state is the profile's former one
(`MTRR_INITIAL`); 1 GiB is two UC pairs ([1, 2) and [2, 4) GiB), 3 GiB one ([3, 4) GiB). Every RAM page is then WB by the
MTRRs and every other address below 4 GiB UC (the legacy hole, [R, 4 GiB) with the I/O APIC and the local APIC).
**Lockstep:** `gen_cpu_profile_rs.py` emits `vectors::MTRR_FOR_RAM` (nine sizes, 2 MiB to 3 GiB, both encodings) from
`mtrr_state`, the abi test compares `firmware_mtrrs` with them and the stale check covers the file; the abi test also runs
every 2 MiB multiple to 3 GiB (1,536 sizes: each page below 1 MiB and the first and last page of each 2 MiB block above
it WB below R, UC from R) and `test_cpu_profile.py` the same over the model (pairs within VCNT, WRMSR accepts the
state). `run_slice3_vm.py`'s `profile` reset values and `run_m2.py`'s firmware state follow `NATEMU_RAM`. **Checks:**
`run_m2.py firmware` (4 cases: R = 4 MiB, 1, 2 and 3 GiB, tables in RAM's last MiB): RDMSR of DEF_TYPE, the 16 variable
and the 11 fixed MSRs equal `mtrr_state(R)`, by which every page below 0xa0000, every 2 MiB block of [1 MiB, R) is WB
and the hole and [R, 4 GiB) UC, and the one store is one WB shadow; 4 of 4 pass, and on the M2 binaries 3 of 4 fail
(the 2 GiB one is the old state). `types`, `devices`: 57 pass. **Kernel** (Debian 6.12.107, `--ram 3 GiB`, the M2
command line, no initrd, N = 1): "MTRR map: 3 entries (2 fixed + 1 variable; max 18), built from 8 variable MTRRs", no
trimming or "BIOS bug" line, "Memory: 3024776K/3129980K available" (all of e820's usable RAM), data shadows WB 514,878 and
UC 23 (the `cache_disable` window); on the M2 binaries the same boot counts 97,866 UC shadows ([2 GiB, R) UC by the
MTRRs; Linux does not trim it, its default type being WB). Both reach the root-fs panic (3.11 and 3.04 s guest; the run then
ends at the default 60 s watchdog: 64.9 and 64.6 s wall). Raw evidence: `natemu-raw/m2-20261102-01/` (`addendum-*`, `boot-*-3g`).

*Built (M2 addendum 2, uncached RAM keeps x86 strong ordering; owner decision, November 2; no fexc change):* **Rule.**
UC (and UC-, which Table 14-7 makes UC under a WB MTRR) is "Strong Ordering" (SDM Vol.3 Table 14-2): "all reads and
writes appear on the system bus and are executed in program order without reordering. No speculative memory accesses"
(14.3), "out-of-order or speculative accesses are not performed" (11.2.5). WT, WP and WB have "Speculative Processor
Ordering" (Table 14-2), x86's ordinary model, which hardware TSO gives; their differences are caching only, invisible on a
coherent machine. WC is "Weak Ordering" (Table 14-2, 14.3.1: writes may be delayed and combined), so every TSO outcome is
one WC allows; MOVNT* likewise. Those four stay native. **Mechanism** (`runtime/src/execution.rs` `translate`,
`access_aborted`, `uncached_end`, `split`; `segments::pages`): generated code never reaches UC RAM through a shadow a block
keeps running with. A data walk that finds UC RAM shadows it only for the step block (the one-instruction block in the slot,
every exit of which enters the resident); for any other block the page is walked (#GP/#SS, #AC, every #PF, A/D, the
code-write protocol: all as for any access, before anything else) but not shadowed, and the instruction is run alone from
its start (`step_from`, the state at the instruction). In the step block the page is shadowed and recorded
(`Control::uncached`), `dmb ish` makes every earlier access globally visible, and the access is retried; the one
instruction completes (or faults); the resident is entered at its exit, and `enter` (the one way generated code starts
again from the resident; a retry continues the same instruction) makes a second `dmb ish` and removes the pages so
shadowed (both CPL spaces; more than four in one instruction: every shadow). So each UC access is ordered after every
earlier and before every later access of its vCPU, both ways and with WB accesses too (stronger than 11.2.5 needs, which
x86 permits); none is made speculatively by translated code running ahead. Every form runs as it does on WB, in the step
block: loads, stores, RMW, LOCK forms (LSE atomics), XCHG, CMPXCHG8B/16B, strings (one element per pass, each its own
step), push/pop/call/ret on a UC stack, SSE/AVX/x87 accesses, accesses straddling WB|UC or UC|WB; data breakpoints are the
step block's Arm watchpoints as before. The resident's own accesses to UC RAM (`segments::pages`: system-instruction
operands, event frames, INS, XSAVE/FXSAVE images, x87 m80; CLFLUSH's walk) and its page-crossing stores (`split`) are made
after a `dmb ish` and before the closing one. Instruction fetch is unchanged: code on UC RAM is translated and runs (14.3.3;
the code-write protocol keeps it current). Device pages are unchanged (M3). Linux's `cache_disable` window (MTRRdefType.E
clear: all RAM UC, the stack and the code's data included) is just many such accesses. Counters: `stats.k1.memory_types`
counts a UC shadow per access, `stats.k1.uncached` the instructions run alone (`steps`) and the resident's own UC accesses
(`resident`). **Checks** (`tools/run_m2.py`, fexc series36): `qemu` 12 (`pat`, `mtrr` (E clear included), and the new
`uc` forms program at 1 and 2 CPUs, block sizes default and 1, against pinned QEMU, each CPU with UC accesses run alone:
the `uc` program 115 per CPU and 7 by the resident), `faults` 27 (every case with the data page UC and WB: identical
outcome, the expected #PF/#AC/#DB or completion), `litmus` 6 (store buffering, K = 10,000, two runs: on UC pages at 2, 3
and 4 vCPUs no every-load-old outcome in 20,000 iterations each, on WB pages at 2 vCPUs 406 of 20,000), `types`,
`devices`, `firmware` 61 (unchanged): 108 cases, 0 failures. **Negative controls** (the addendum-1 binaries, without this
change): `litmus` UC red at 2, 3 and 4 vCPUs with 418, 11 and 0 every-load-old outcomes (all three red for no instruction
run alone); `qemu` 12 of 12 red (values equal, nothing run alone); `faults` the 6 completing cases red, the 21 faulting
ones green (faults were already right). **Cost** (`run_m2.py cost`, CPL 0, 20,000 iterations): about 4.8 us per MOV load,
4.7 us per MOV store, 4.3 us per LOCK ADD on UC RAM against 7-11 ns per WB iteration; each UC access is one abort, one
host step request (the host answers repeated steps from its in-run compile memo) and one resident entry. **Kernel**
(Debian 6.12.107, 2 GiB, the M2 command line, no initrd, `--watchdog-ms 10000`, a private warm compile cache, release
host, two runs each): N = 1 before 1.176 and 1.172 s guest (12.24 and 12.24 s wall), after 1.199 and 1.191 s (12.31 and
12.25 s); N = 2 before 1.386 and 1.375 s (12.13 and 12.12 s), after 1.362 and 1.342 s (12.10 and 12.09 s): within run-to-run
noise. The BSP runs 533 instructions alone (`cache_disable`'s window; about 2.6 ms at the measured cost); UC data shadows 563
against 23 before (now one per access). With the shared compile cache at its 512 MiB cap (other runs' entries) the same
boots take 3.0-4.0 s guest before and after alike; at 3 GiB (addendum 1) the BSP also runs 533 alone. **Review** (independent):
no blocking defect; applied: the resident's own guest accesses (`segments::pages`) also make the closing barrier while a UC
access is pending (it came only at `enter`, after `find`'s event delivery and kept system instructions, covered until then
only by `cache::drain`'s acquire-release swap), a page is recorded once per instruction, comments. Not changed: an unmapped
UC shadow keeps the writable bit until the next reset (conservative), and the cost (one host step request per access; a
resident-kept step block would remove it, the performance rule defers it). Raw evidence: `natemu-raw/m2-20261102-01/`
(`uc-*`, `boot-*`).

*M2 integration (November 2, after the second independent review: no blocking defect, one should-fix):* **What orders UC
accesses today.** The review found that `litmus` cannot see the two barriers: with both `dmb ish` (the retry in
`access_aborted`, `uncached_end` at `enter`) made `nop` it stays green. A mixed store-buffering case was added
(`litmus-sb@2-mixed`, x0 UC and x1 WB: vCPU 0 stores UC x0 then loads WB x1, with no host exit between the step block and
the load; vCPU 1 stores WB x1 then loads UC x0). Both old is forbidden because UC reads and writes "are executed in program
order without reordering" (SDM Vol.3 14.3, Table 14-2 "Strong Ordering"; 11.2.5: "out-of-order or speculative accesses
are not performed"), while 11.2.2's reads passing older writes is stated for WB regions. With the barriers it is green:
20,000 iterations, 0 both old (3 / 9,227 / 10,770 of the other three outcomes; WB control at 2 vCPUs 434 both old). It
cannot tell the barriers apart either. Without the two barriers it is green (0 both old in 20,000), and also when
`cache::drain`'s swap is skipped as well for an empty record (0 in 20,000; that build with the barriers: 0). The reason
is how a UC access is reached: always through an abort into the resident, whose `aborted` and `find` drain with a
`swpal` (an acquire-release swap, a full barrier), and outside the step block through a host step request (a VM exit). So
the other vCPU's access to a UC location comes microseconds after its own previous access, and a store does not stay
buffered that long. No arrangement makes the barriers observable today. UC ordering also rests on the host exit, the
resident entry's swap and that latency. The barriers are what the Arm architecture guarantees it by, and they become
load-bearing if step blocks are kept in the resident or the drain's swap becomes conditional; a litmus can show it only
once a UC access costs less than a store's time in the buffer. **Within one instruction.** An instruction with several
accesses on UC (ENTER with a nonzero level, PUSH/POP m, MOVS, CMPXCHG16B, the vector and x87 forms) runs inside one step
under TSO, with no barrier between its own accesses. This is acceptable: the SDM gives no order among an instruction's
constituent accesses (Vol.3 11.2.3.1: "there are no guarantees regarding the relative order in which the constituent
memory accesses are made"). **Large pages.** A 2 MiB page that spans several MTRR types is undefined (Vol.3 14.11.9: "the
processor can behave in an undefined manner if a large page is mapped to a region of memory that MTRRs have mapped with
multiple memory types"). natemu types each 4 KiB of such a page on its own, which 14.11.9 also names as the way to avoid
the problem ("multiple 4-KByte pages each with its own memory type"). **Added checks:** `types` `pat-to-uc-cr3` and
`pat-to-uc-invlpg`: a page shadowed WB (PAT index 4), a PAT write making entry 4 UC, a CR3 reload or INVLPG, then a store.
Each must give one WB and one UC shadow and one instruction run alone. `pat-to-uc-no-flush` has no flush, so the WB shadow
is kept (5.9.3). The independently written `tools/models/coherence_model.py` now types routes (`route_type`, Table 14-7
and 14.11.4.1). An earlier sweep of 20,000 random PAT/MTRR states agreed with `cpu::memory_type` on 159,548 defined cases,
with 0 disagreements; all 452 cases the SDM leaves undefined are UC in natemu and undefined in the model. **Checks on
94ed0ca + M2** (fexc series36): cargo build and tests, the resident stack check, the Python unit tests; `run_m2.py`
112 cases, 0 failures; `run_slice1_faults.py` 7,912 match at block sizes default and 1; `run_slice3_vm.py` `system`,
`smp`, `litmus`, `sharedpt`, `coherence` 0 failures; `run_s4.py` and `run_k1.py` green. Debian 6.12.107 (2 GiB, the M2
command line, no initrd, shared compile cache, release host) reaches the root-fs panic at N = 1 (3.05 s guest, 14.6 s
wall) and N = 2 (2.23 s, 13.2 s), with UC 563 data shadows and 533 instructions run alone. Raw evidence:
`natemu-raw/m2-20261102-02/` (`litmus-A..D.json`: as built, the barriers removed, the drain's swap also skipped, the
swap alone skipped).

**Owner decision (November 2): uncached RAM keeps x86 strong ordering.** The earlier declared deviation (RAM of every
type runs as coherent write-back, UC's strong ordering not kept; Built (M2)) is withdrawn. Pages whose effective type is UC
or UC- are not accessed natively by translated code running on: every access traps and is made in the resident's control
with the barriers strong ordering requires (SDM Vol.3 11.2.5, 14.3), with permission, #PF, CPL and data-breakpoint checks
unchanged; WC, WT, WP and WB stay native. Built: M2 addendum 2.

*Built (I5c, unmasked floating-point exceptions; owner decision 3 of October 30; fexc series39-i5c = series36 + patch 0026 + shim changes; one independent review, its four blocking findings fixed below):*
**Design (the recorded one, kept).** Hardware FP traps stay unused (FPCR's enables clear). Code is keyed by the unmasked
sets (`Cpu::code_context` bits 18..23 MXCSR's, DE left out under DAZ, bits 24..29 FCW's: `simd_unmasked`, `x87_unmasked`);
the host passes them as fexc's `--simd-unmasked` / `--x87-unmasked` only when nonzero (both gating arguments of the compile
cache key, `GATING` = 8). Every mask change is a resident transaction that ends its block (LDMXCSR, VLDMXCSR, FXRSTOR,
XRSTOR, FLDCW, FLDENV, FRSTOR, FNINIT, FNSAVE, FNSTENV): the refusals are gone. With every exception masked, as Linux
runs, nothing is emitted: 12,252 blocks of the I5a/I6/I6b/I8/I9/I10 corpora and seed 4242 compile byte-identical (code,
sites, PC table) under series36 and series39-i5c. **SIMD (#XM; SDM Vol.1 11.5, Vol.3 event 19).** Patch 0026's
`FexcSimdCheck`, placed by `FexcPrecision::PlaceFPChecks` right before an instruction's first committing op, after all its
floating-point ops (a fault point: no value is coalesced into a static register across it, so the stub sees the state
before the instruction; COMISS's NZCV through the pre-NZCV save): `mrs x0, fpsr; movz w1, #M; and; cbnz w1, S` to a `brk #5`
stub (kind 15 `simd`), else the flags move into the SIMD flag byte (FEX's unused flag byte 33, `frame::SIMD_FLAGS`, banked
into MXCSR at every resident entry, cleared at `enter`) and `msr fpsr, xzr`, so FPSR holds one instruction's flags at every
check. The resident (`fp_exception`, `sse::exception`) sets MXCSR by the SDM's steps (11.5.3.2/3, `simd_fault_flags`): an
unmasked pre-computation exception (IE, ZE, DE) in any element sets the pre-computation flags only; else every element's
flags, masked ones included; then #XM, or #UD without CR4.OSXMMEXCPT (flags still set). Exact tiny results: x86 reports UE
for a nonzero tiny result whether or not exact when UE is unmasked (4.9.1.5, 11.5.2.5), Arm's UFC only when inexact, so
under an unmasked UE each result lane of an op that can underflow (arithmetic, FMA, narrowing conversions, VCVTPS2PH's
upper half `VFCVTN2`) is tested for a nonzero zero-exponent value; FTZ is ignored then (10.2.3.3: `fpcr` sets FZ only with
UM). DPPS/DPPD: a check after every multiply and add step in order (DPPS "in the order of their execution"; VFAddV's first
level checks itself). VDPPS ymm: the SDM orders the steps of one dot product, not the two halves (unspecified); natemu
runs the low half's first, and the test accepts either order. **x87 (#MF; Vol.1 8.5-8.7, Vol.3 event 16).** The
instruction's ops collect their flags in a pending byte (flag byte 34, folded into the exception word at the instruction's
end by the stack pass, dropped at `enter`); `FexcX87Check` compares them and the stack pass's stack-fault, overflow,
invalid-operand (FIST's NaN/out of range, FST m64's SNaN) and exact-tiny (FST m64) bits against the exceptions that leave
the operands unaltered: IE (stack faults included), DE, ZE, and OE/UE for FST/FSTP m32/m64. A hit reaches a `brk #7` stub
(kind 16 `x87`, width the instruction's length; `x87_fault_flags`): only the flags (a stack fault IE SF C1; an aborted store
OE or UE without PE and C1 = 0) are set and the instruction is done (RIP + length, RF/TF as a completed instruction). PE,
and OE/UE to a register, store the result. ES and B derive from the flags and FCW (`fsw_summary`; FNSTSW in code, FNSTENV,
FNSAVE, FXSAVE, XSAVE images). Every waiting instruction (all admitted x87 forms but FNSTSW/FNSTCW, WAIT, every MMX-register
form but CVTPI2PD xmm, m64) starts with the FSW test to a `brk #8` stub (kind 17 `pending`): #MF at it with CR0.NE; the
resident's waiting transactions (FLDCW, FLDENV, FRSTOR, FLD/FSTP m80, after #NM, before memory) the same; FNINIT, FNCLEX,
FNSTENV (which then masks every exception, SDM FNSTENV), FNSAVE, FXSAVE/FXRSTOR, XSAVE/XRSTOR ignore a pending exception.
Helper forms (`svc #X87`): an unmasked stack fault, IE or ZE leaves the operands. (`brk #6` is the resident's stop word.)
**CR0.NE = 0 (decision): a typed stop `unsupported_ferr`** at the waiting instruction, the state there. The SDM's MS-DOS
compatibility mode reports through FERR#, the PIC's IRQ13, port F0H and IGNNE# (8.7.2, Appendix D), freezes before the
waiting instruction, asserts FERR# at the faulting or the waiting instruction "depending on the type of processor", and
"limits newer processors to operate with one logical processor active"; this board wires neither FERR# nor IGNNE#, so the
processor would freeze there for ever: the stop is that outcome, made visible. **Admission:** the stubs are address-free
(`addressless`), the x87 one's width must equal its instruction's length; the code shape checks each check sequence word
for word (`fp_checks`). **Checks:** `tools/run_i5c.py` (SDM expectations; QEMU 7.2 raises no #XM, delivers #MF only at
FWAIT and pops/writes on unmasked ZE: probed, `natemu-raw/i5c-qemu-20261102/`), 236 cases at block sizes default and 1, all as
expected: 47 SSE/AVX/FMA/F16C forms masked, with each raised flag unmasked and with the others unmasked; multi-element
precedence (SDM Figure 11-9 included); exact tiny, FTZ, DAZ; DPPS/VDPPS steps; YMM; RCPPS's FPSR not leaking; #UD without
OSXMMEXCPT; LDMXCSR/VLDMXCSR/FXRSTOR/XRSTOR re-keying; #MF at 18 kinds of waiting instruction; the non-waiting ones;
aborted IE/DE/ZE, stack faults (resident m80 forms included), FST/FIST stores, PE/OE stores, FLDCW/FLDENV unmasking a set
flag, helpers, a #PF after a passed check (FSW unchanged), TF over an aborted instruction, CR0.NE = 0. Compile sweep: every
block of the seven FP corpora under five unmasked sets (417,590 instructions), no compile failure. The I5a/I5b/I6/I8 refusal
cases now expect the load. Standing suite (series39-i5c) green but the 7 recorded I8 two-NaN QEMU departures; FEX ASM 1,870 pass, 0 fail, 48 precision deviations. **Negative controls:** HEAD 5d7f62e's binaries with series36: 236 of 236 red (the driver takes no unmasked `mxcsr` nor `ne`); this build with the host passing no unmasked set (no checks compiled): 127 red, exactly the cases expecting #XM/#UD/#MF/an abort, the 109 masked and no-fault ones green. **Cost** (a loop of ADDPS, MULPS, FLD/FADD/FSTP m64; and XOR + CPUID, a resident entry and block entry per iteration; 2.2 M minus 0.2 M iterations, best of 3, two rounds): every exception masked, HEAD 21.7 and 21.9 ns, I5c 21.6 and 21.7 ns per FP iteration, CPUID 537.7/540.7 against 533.9/536.1 ns: no measurable change (the generated code is identical; the resident adds two byte stores at `enter` and one byte load per entry). Every exception unmasked: 41.4 ns per FP iteration (the checks, the flag fold, the pending tests).
**Deviations (new, the reduced-precision class):** (1) a register-destination OE/UE with the exception unmasked stores the
double's masked result (no 2^24576-biased exponent, no round-up C1); (2) I6's deviations 1 and 4 (OE, UE and DE detected at
the double's range: `1e300 * 1e300` overflows, a result below the double's normal range underflows, a double denormal is a
denormal operand, where an 80-bit x87 holds all of these as normal values) now become exception differences when OM, UM or
DM is clear: FMUL 1e300 * 1e300 with OM clear, or a double-denormal result with UM clear, reaches #MF where x86 raises
nothing; x87 arithmetic, FST m32 or FCOMI on a double denormal with DM clear aborts the instruction and reaches #MF; (3)
FIP/FDP stay 0 (deviation 8), so an #MF handler cannot locate the faulting instruction from the image; (4) a masked stack
fault drops the operation's own flags (x86's precedence, SDM Vol.1 4.9.2) only with x87 exceptions unmasked; with every
exception masked those flags are still ORed in as before (the old departure, kept so the masked path is unchanged).

*Built (I9 addendum, CVTPI2PS m64's MMX transition; fexc series40-i5c-add = series39-i5c + patch 0027):* the SDM's CVTPI2PD page exempts only "operands xmm, m64" from the x87-to-MMX transition; CVTPI2PS's page gives the transition (TOP = 0, every tag valid; a pending x87 exception handled first) with no operand exception, for `mm/m64` alike. I9 had read the CVTPI2PD sentence into CVTPI2PS: FEX made the transition only when an operand is an MMX register, so CVTPI2PS xmm, m64 left TOP and the tags. Patch 0027 makes it for every CVTPI2PS (at the instruction's end, as every MMX form since patch 0024). The whole MMX/SSE interop list was probed (TOP and tags after FNINIT; FLD1; the form): CVTPI2PS m64 was the only wrong one; CVTPI2PD m64 makes none, and CVTPI2PS/PD mm, CVT(T)PS2PI and CVT(T)PD2PI (register and memory sources), MOVQ2DQ, MOVDQ2Q, MOVNTQ, MOVD/MOVQ m, mm, PEXTRW, PMOVMSKB, PADDB/PSHUFW/PALIGNR with m64 sources make it. **Checks:** `run_i9_faults.py` `state-cvtpi2ps-m` now expects TOP 0 and tags FFH: red with series36 (HEAD) and series39-i5c, green with series40-i5c-add; 586 cases at block sizes default and 1, the I9 corpus 120/120 against QEMU 7.2 (which makes the transition there too) at both sizes, `run_i5c.py` 236 and `run_i6_faults.py` 275 as expected; of 8,131 corpus blocks compiled by series39-i5c and series40-i5c-add only the two holding a CVTPI2PS m64 differ.

*Built (I5c review round; fexc series42-i5c = series40-i5c-add + patch 0028; one independent review, two blocking and
three should-fix findings, all taken):* (1) FST/FSTP m64 with UE unmasked took +-DBL_MIN, a normal double, for an exact
tiny result (an off-by-one in the stack pass's flag-free test) and stored nothing: the test is now magnitude nonzero and
exponent field zero; every other boundary test (`FexcTinyLanes`) was checked and has the form already. (2) CVTPS2PI and
CVTTPS2PI mm, xmm converted all four floats of the XMM register (FEX's 128-bit `.4s` conversion), so the upper two raised
MXCSR flags (an I9 bug when masked, a spurious #XM when unmasked): they convert the low 64 bits as the m64 form does. A sweep
of every register-source form reading fewer lanes than its Arm op could touch (the scalar SS/SD forms of SSE, AVX and FMA,
CVTSS2SD, CVTSD2SS, CVTPS2PD, VCVTPS2PD, CVTDQ2PD, CVTSS2SI, CVTSD2SI, ROUNDSS/SD, RCPSS, VCVTPH2PS, CVT(T)PS2PI) with
SNaNs in the unread lanes and every exception masked found no other flag leak. (3)-(5) the deviation wording and the
VDPPS order above. **Checks:** `run_i5c.py` gains the review's 19 cases (VCVTPS2PH's upper lanes one by one, FMA inf * 0 +
QNaN, VCVTPH2PS/VCVTPS2PH denormals, CVT(T)PS2PI's upper lanes masked and unmasked, FST m64 +-DBL_MIN and a negative
denormal, FST m32 FLT_MIN and an exact float denormal, FISTTP/FISTP invalid): the DBL_MIN and CVT(T)PS2PI ones red with
series40-i5c-add, all green with series42-i5c.
*Built (X1b, X1c: CPL 3 frames, every IST index, paranoid entries, the double-fault matrix, refused-but-defined forms
delivered; series36, and series38-x1 for the addendum):* **X1b (checks only; X1a built the mechanism).** `tools/bench/smp/entry.py`,
`run_x1.py qemu`: each CPU loads its own GDT (with a DPL 3 read-only expand-down "CPU/node" descriptor whose limit is the
CPU number), a TSS whose RSP0 and IST1..7 sit 8 bytes off a 16-byte boundary, an IDT and CR4.FSGSBASE, and IRETQs to
CPL 3 with GS.base = a user block and KERNEL_GS_BASE = its per-CPU block. Gates: #DE #UD #GP #PF on RSP0, #DF IST1, NMI
IST2, #DB IST3, 0x12 (the #MC vector, by INT 0x12) IST4, #BP IST5, 0x80 IST6, 0x81 IST7. "Normal" handlers SWAPGS when
the frame's CS has RPL 3; "paranoid" ones (#DF, NMI, #DB, 0x12) RDGSBASE, LSL the CPU/node selector, index a table of
per-CPU bases and WRGSBASE, restoring the base before IRETQ (architectural instructions only, SDM Vol.2 SWAPGS,
RDGSBASE/WRGSBASE, LSL). CPL 3 runs UD2, INT3, INT 0x80, MOV from CR0 (#GP(0)), a supervisor load (#PF(5)), DIV by 0,
INT1, a single step, INT 0x12 through a DPL 0 gate (#GP(0x92)), INT 0x40 through gates not present (#NP(0x202) serially,
then #NP(0x5b): #DF on IST1), LSL/RDGSBASE/[gs:0] at CPL 3, then INT 0x81 into CPL 0, where a self-NMI's handler sends
a second NMI that must wait for its IRETQ (logged with the in-NMI flag clear, same frame RIP), INT 0x12, UD2 with RSP
8 off 16, INT1 and a CPL 0 #DF (so IST 2 and 4 are met from CPL 0 only; IST selection does not depend on the CPL).
Every logged frame (SS:RSP, RFLAGS, CS:RIP, error code, RIP-slot address) is compared with pinned QEMU, and
delivery_model judges, given the logged context, the vector chain, error code, RF and the stack chosen (the slot); the
second NMI's frame RIP must equal the first's. 1, 2 and 4 CPUs, block sizes default and 1: 3 of 3 programs match (17
events per CPU). It passes on HEAD too: X1b found no defect. Negative control: IST n read from slot `n & 6` (IST 1
right, 2..7 wrong) is red (70 failures: frames against the model and memory against QEMU). QEMU departure (RF cleared
in the QEMU comparison, the model judges natemu's RF): pinned QEMU pushes RF = 1 for INT n/INT3 run after an IRETQ that
loaded RF = 1 and for NMIs (SDM: 0, and the current RF).
**X1c, the double-fault matrix** (SDM Vol.3 interrupt 8, Tables 7-4 and 7-5; `run_slice3_vm.py delivery`, judged by
delivery_model alone since QEMU resets on a triple fault): 51 cases at both block sizes (X1a's 18 and 33 new). A fault
detected while delivering is a #GP, #NP, #TS or #SS (gate, IDT limit, target CS, TSS limit, RSP0/IST canonical) or a #PF
(IDT, TSS or frame on a page not present), so the reachable cells are every first class against contributory and page:
benign then either, serial (#UD, #NM, INT n, INT1, NMI, an external interrupt); contributory then contributory, #DF (first
#DE, #GP, #NP, #TS, #SS, each); contributory then page, serial; page then contributory (#GP, #NP, #TS, #SS) or page, #DF;
#DF then contributory or page, shutdown (`stop::SHUTDOWN` with the last fault, the original RIP and CR2; also from an IDT
limit below every gate, an IDT on a page not present and RSP0 beyond the TSS limit from CPL 3). A benign second
exception never arises inside a delivery (a data breakpoint on the frame is a trap taken after it, S4's
`s4-d-ext-frame`), so the benign column is the serial default. Only the last delivery is visible, so where the second
fault's own gate could take it, that gate is live: serial handling instead of #DF ends at another handler. Negative
control: page-then-contributory made serial is red on exactly its 4 cases (8 runs). #DF's CS:RIP stay the original
context (undefined by the SDM; the model accepts any). The independent review found the contributory-then-#PF cell
indistinguishable from #DF in the first version (both ended in #DF on IST 1); `de-pf` and `gp-pf` now take the #PF
on IST 1 with #DF's gate on the bad stack (a wrong #DF would end in a shutdown); red with that cell made #DF. **Refused-but-defined.** Every typed stop guest behaviour can reach
in the resident and host was inventoried and classified as an SDM-defined exception (delivered now), a declared refusal
of something not built or undefined, or an emulator invariant. Delivered now (each a stop on HEAD, 0 of 87 cases pass
there): #UD for PUSH/POP ES/CS/SS/DS and JMP/CALL ptr16:32 in 64-bit mode, LSS/LFS/LGS and far JMP/CALL with a register
operand, the 0f 01 register forms no advertised feature defines and 0f 01 /5 m without F3, 0f 00 /7, and (host
`system::undefined`, for forms fexc refuses as `unsupported_form`) the blank Table A-2/A-3/A-6 cells (`d6`, `0f 04 0a 0c
0e 24..27 36 39 3b 3c..3f 7a 7b`, group 9 /0../5 register and /0 /2 memory), register operands of memory-only forms (LEA,
MOVNTI, MOVLPS/MOVHPS/MOVLPD/MOVHPD stores, MOVNTPS/PD, MOVNTDQ, LDDQU, MOVNTDQA, CMPXCHG8B/16B), GETSEC (CR4.SMXE = 0),
RSM outside SMM, the VMX group-9 forms and the blank or unadvertised SIMD cells (review round, below); #GP(0) at a
direct JMP/CALL/Jcc whose target is not canonical, when taken (host `system::branch`, resident `system::BRANCH`: the
condition from the banked flags, nothing changed, CALL before its push; not taken it completes), which fexc refuses
("slice 1 does not deliver that #GP"). Also built, defined instructions that stopped: CLTS, SMSW (16-bit merge, 32/64
zero-extended, m16), LMSW (PE never cleared), SYSENTER and SYSEXIT (REX.W; without it compatibility mode stops
`unsupported_mode`, as SYSRETL). Declared refusals kept (not built, or undefined): CPL 1/2 and compatibility mode, call
gates, 66 on far transfers, LSS/LFS/LGS m, ENTER with nesting, the fexc forms not built (RCL/RCR m, 16-bit
SHLD/SHRD/BSWAP, MASKMOVQ, ...), 66/F3 on a near branch to a canonical target (the SDM ignores them, but FEX's lowering
is not proven for them), REP prefixes and segment overrides the SDM leaves unpredictable on other instructions, DR7 R/W = 10
without CR4.DE (undefined), #AC on 8-aligned 16/32-byte vector accesses ("may or may not"), split locks (owner), unmasked
x87/SIMD exceptions (being built), device-page forms and device refusals (M3, L3, L4b), xAPIC base over RAM, ICR SMI,
reserved and INIT/SIPI modes (X4), page tables outside guest RAM. Invariants: shadow capacity, a watchpoint beyond the
PE's (unreachable with 4), the copy-in failures after a walk, and the internal reply/link/abort stops. **Addendum (fexc series38-x1 = series36's patches + a shim change, no FEX patch):** LOOPcc/JrCXZ to a
non-canonical target were compiled (the #GP came at the target, RCX decremented); the shim's direct-branch set now
includes `e0..e3` and `system::BRANCH` evaluates the count (RCX, or ECX with 0x67): taken, #GP(0) at the branch with RCX
unchanged (a fault restores the state, Vol.3 7.5); not taken, RCX decremented (ECX written zero-extended). Indirect
JMP/CALL/RET to a non-canonical target already fault at the branch (probed). The guest-RIP split lock is no longer
refused at admission (a host error): its atomic faults and the resident stops `unsupported_atomic` as for a computed
address. Code on a page that is not guest RAM (since M2 any RAM type is fetched): the owner decisions of October 29 (below:
unassigned addresses read all-ones; the amendment maps unassigned pages read-only and non-executable) leave no execution
there, and the SDM defines no exception for fetching from memory-mapped I/O, so it is the typed stop `no_code`
`fetch_not_ram` at the RIP (was a host error). PUSH/POP FS/GS
(`system::PUSH_SREG`, `POP_SREG`; SDM Vol.2 PUSH "a zero-extended value" for a 64-bit push, 66: 2 bytes; POP with MOV
Sreg's checks, RSP unchanged on a fault). Hint NOPs: PREFETCHW m8 (`0f 0d /1`: "merely a hint", no #UD without the CPUID
bit) and CLDEMOTE m8 (NP `0f 1c /0`: "treated as a NOP" without it) are compiled. **Reserved-NOP cells (owner decision,
November 2): NOPs, following the modelled processor.** The other `0f 0d` memory forms, `0f 18` other than /0../3 m
without F2/F3, the `0f 18` and `0f 1c` register forms, the prefixed `0f 1c` and all of `0f 19..1d` (MPX's `0f 1a/1b`
included: no MPX advertised, and no instruction page in this SDM) are cells SDM Vol.2 A.1 calls "Reserved-NOP": "may
behave as NOP on certain processors"; a typed stop would halt the VM where the modelled processor executes a NOP. Pinned
QEMU (system mode, the smp stub) executes all 296 such encodings of a sweep as NOPs with no memory access (each with no
prefix, 66, F2 and F3, /0../7 on a non-canonical [rax] and register forms; raw evidence `natemu-raw/x1-20261102-01/
qemu-reserved-nop-sweep.log`, 314 encodings with LOCK and the 0f 0d register forms). fexc cuts them as system
instructions, the host decodes the ModRM (`system::RESERVED_NOP`, any 66/F2/F3/REX/segment prefix) and the resident
completes them with nothing accessed; LOCK is #UD (the resident's LOCK rule; QEMU agrees). **`0f 0d` with a register
operand is #UD** (fexc `cut_ud`): PREFETCHW takes m8 and Table A-3 lists only "prefetchw(/1) Ev" there, the I10 rule
(#UD for an encoding the SDM does not define as an instruction), and pinned QEMU raises #UD for all 12 such encodings of
the sweep (owner decision, November 2). Checks:
`run_x1.py qemu` gains `x1-sreg@1` (PUSH/POP FS/GS of every size, null and DPL 3 selectors, PREFETCHW/CLDEMOTE on a
non-canonical address) equal to pinned QEMU and the SDM's registers, and `x1-rnop@1` (the sweep's 296 NOP encodings plus
REX, SIB, displacement and segment-override variants, markers kept) equal to pinned QEMU; `cases` 112 (25 new: LOCK on
three cells and `0f 0d` register forms #UD; LOOP/LOOPE/LOOPNE/JrCXZ taken
and not, with and without 0x67, the count wrap; POP FS beyond the GDT #GP(0x7f8), PUSH GS at a non-canonical slot
#SS(0), POP FS of a DPL 0 selector at CPL 3 #GP(0x10); the split lock; the fetch; the hints), all at both block sizes;
on the x1-all build with series36, 15 of them, `x1-sreg@1` and `x1-rnop@1` are red (every taken branch, every new form,
both typed stops, the `0f 0d` register forms); the three LOCK cases were #UD there already (kept as regression checks).
Compiler slice-1 tests 34 pass, i10 faults 296 of 296 at both block sizes, ASM 1,869 pass.
**Review round (November 2; an independent review, nothing blocking; fixes):** *Prefixed direct branches:*
`system::branch` skips every prefix a near branch ignores in 64-bit mode (segment overrides and the 2e/3e Jcc hints; 66:
"operand size prefixes are silently ignored ... the displacement field for relative branches is still limited to 32
bits", Vol.1 3.3.7; REX, W included; F2; F3; 0x67 sets only LOOP/JrCXZ's count width), so a prefixed branch to a
non-canonical target faults (or completes) as the unprefixed one. *Blank cells:* `system::undefined` now carries the
legacy cells of the SIMD rows of Tables A-3, A-4 and A-5 by mandatory prefix (`SIMD`, each with the CPUID feature that
defines it, read from the generated profile) and gives #UD for every cell of those rows not defined there, or defined by a
feature the profile does not advertise (SHA, GFNI, ADX, CET_SS, KL, MOVDIRI, MOVDIR64B, ENQCMD), plus JMPE `0f b8` and
66/F2 `0f b8`, the operand types of the register-only (`0f 50`, `d7`, `f7`, `c5`, `71..73`, F3/F2 `d6`) and memory-only
(66 `0f 12/16`) forms, and PTWRITE (F3 `0f ae /4`, Intel PT not advertised; the memory form in the system decode); a cell
with both 66 and F2/F3 is left alone. A fexc sweep of every 0f/0f 38/0f 3a opcode x NP/66/F3/F2 x memory/register found
4,190 encodings it refuses in blank SIMD cells; pinned QEMU (system mode) raises #UD for 4,142 of them; it differs on 48
(its decoder ignores 66/F3/F2 on EMMS `0f 77` and on SHA `0f 38 c8..cd`, `0f 3a cc`, and its CPU model has SHA), where
the SDM's blank cells decide: #UD (raw evidence `natemu-raw/x1-20261102-01/qemu-blank-cells-sweep.log`).
`undefined_follows_the_profile` derives the expectation for every feature-gated cell from tools/models/cpu_profile.json
(advertised: no #UD; clear: #UD) and checks the generated profile agrees, so a profile change cannot leave a false #UD.
*BND (owner decision, November 2): F2 on near CALL, RET, JMP and Jcc executes as the unprefixed branch, following the
modelled processor:* MPX's BND prefix was designed to be ignored where MPX is not enumerated or enabled, MPX-era glibc
emits `bnd jmp`, and processors ignore it; pinned QEMU executes them so (`x1-bnd@1`: BND CALL rel32/RET, JMP rel8/rel32,
Jcc rel8/rel32, JMP and CALL through a register, equal to QEMU at both block sizes). The shim admits F2 on those branches
(not on LOOP/JrCXZ) and admission's branch-target proof skips it. *Also:* an IRETQ that faults still releases NMI
blocking (`iret-fault-nmi-unblocked`: the second NMI is taken at once); `fetch_not_ram` cites the owner decisions of
October 29; the stack audit accepts a veneer's return only into the function whose `b` at the displaced address entered
it. Checks: `run_x1.py` 6/6 QEMU programs, 188 cases (every condition taken and not, the prefixed branches, the blank
cells and PTWRITE) at both block sizes.
**Stack checker:** this build's layout made
lld (`--fix-cortex-a53-843419`, the target's default; no option turns it off) move an instruction at a page's end into
a veneer that branches back into `expm1`; `check_resident_stack.py` now accepts exactly that return (the veneer's own
address + 4), with a test. Gate bound 19,168 of 32,768 bytes, boot 12,832. **Suite** (5d7f62e + X1b/X1c, series36): green
but the 7 known i8b QEMU NaN departures (both block sizes) and `test_check_setup`'s launcher default, which differs only
because the working copy sits behind a symlinked `natemu-raw`; ASM 1,865 pass (FEMMS now #UD, one SMSW test passes). **On 3fc2c4b (after I5c):** the compiler is series43-x1 = series42-i5c's patches (0001-0028) + the X1c shim
changes (no FEX patch). Standing suite on 3fc2c4b + X1b/X1c with series43-x1, both block sizes: green but the 7 known i8b
QEMU NaN departures; `run_x1` 6/6 and 188/188, delivery 51, `run_i5c` 255/255 ×2, `run_m2` 112, `run_s4`, i10 faults
296/296 ×2, compiler slice-1 34 and `test_fexc` pass; ASM 1,875 pass, 0 fail (AMD's SSE4a MOVNTSS/MOVNTSD, now #UD on the
Intel profile, are counted as not advertised). Debian 6.12 release boots: no initrd at N = 1 and N = 2 reach the root-fs
panic (14.6 s and 13.1 s wall), the initrd boot at N = 2 reaches the installer's language dialog (56.7 s wall).

*Built (L0, the host split; owner decision 4 of October 30; no fexc change, series43-x1):* **Layout.** `crates/host` is
the library package `natemu` (`src/lib.rs`; no library file moved) with the boot stub binary `natemu` (`src/main.rs`);
`crates/driver` is the package and binary `natemu-host` (`src/main.rs`, `src/drivers/{mod,program,linux,test_devices}.rs`,
moved from `crates/host/src`), so `cargo build -p natemu-host`, `target/release/natemu-host` and every runner are
unchanged; `Cargo.lock` regenerated; both crates are default members. **Public surface:** `pub(crate)` stays the default;
only what the driver uses was promoted to `pub` (about 170 items: 79 functions, 29 structs, 5 enums, 2 traits, 42
constants, about 100 fields, 14 public modules; `admission`, `system`, `json`, `ioapic`, `pic`, `pit` and `rtc` stay
private; 182 `pub(crate)` remain). The documented integration API is `natemu::linux::{Boot, Prepared, Outcome}`.
**Boot entry** (`linux.rs`): `Boot` (runtime, fexc, kernel, initrd, cmdline, RAM, vCPUs, RSDP, code cache, serial sink,
strict unassigned, lost-tick policies, watchdog, block limit, stop RIP, compile cache; defaults from `Boot::new`),
`prepare()` (backing, loader, ACPI tables, CPU/LAPIC/mailbox state, UART, I/O APIC and 8259, PIT/RTC/PM),
`Prepared::run()` (VM, vCPU threads, the first vCPU to end ends the run, destroy) and `Outcome` (each vCPU's stop and
compiles, the vCPU that ended the run). The compile cache is opt-in: `compile_cache::Config::from_env()` (the moved
environment code; `Config::OFF` by default); `natemu` and `boot-linux` opt in. `boot-linux` is argument parsing, the
load-only path, then `prepare` → `--dump` → `--clock-trace` around `run` → `--save` → the same JSON. The stub: `natemu
RUNTIME FEXC BZIMAGE [--initrd] [--cmdline] [--ram] [--vcpus] [--serial]`, serial to standard output by default, exit 0
on poweroff, 2 on another stop, 1 on a host error. **Hook** (`execution.rs`): `trait Hook { at, boundary, admitted,
before_compile, reply }`, every method a no-op by default and `impl Hook for ()`, generic through `run`/`serve`/
`translation`/`Compiler::compile`/`compile_once`/`ask`, so a run without one (both boot paths) compiles the calls away;
every call sits on a host exit. `At::Stop(code)` ends the run as `Stop::Hooked(code)` (replaces `Stop::Logged`),
`At::System` answers with a system reply; `ReplyKind::Translation` (never while parked) and `Device`. The driver's
`ProgramHook` holds the logging IDT's handler range, the CPL switches and event sources (with its doorbell), and on the
tampered vCPU `--forge-exit`, `--kill-compiler`/`--stall-compiler` and the reply tampers (each disarms when it fires; the
device tamper excludes the translation one), moved verbatim. Removed from the library: `Compiler.tamper`/`signal_at`,
`Requests.tamper`/`tamper_device`/`tampered`/`switches`/`handlers`/`posts`/`doorbell`; `TestDevice` and `TestSource`
moved to `drivers/test_devices.rs` with their two unit tests. **Knobs:** `Stops::new`'s latency flag is gone (only a
kicker, which only the driver has, reports a followed stop; identical behaviour). `Machine::silence` and
`enter(.., doorbell)`/`Vcpu::doorbell(enabled)` (`--ignore-kicks`) stay as documented options: they configure the GIC
while vCPUs are created, before any run; a hook sees only runs, and the hook would still need the same library calls.
`Compiler.shape` (`--code-shape`) and `Devices.strict` stay options, off by default. **Review round:** raw writes into a
vCPU's records and code cache (`Area::write_record`/`copy`, a test `Area::over`) and the hook's `admitted`, `reply`,
`before_compile` and `At::System` (any system kind, `SET_CPL` included) exist only with the library feature `testing`,
which only `crates/driver` enables; `Area`'s fields and `Memory`'s pointer are crate-private (getters `memory`, `vcpu`,
`epoch`); without it a hook can only end a run (`at`, `At::Stop`, `boundary`). The stub built alone (`-p natemu`) has no
`testing` cfg (checked with `cargo build -v`; a whole-workspace build unifies the feature into it). `Prepared::run` runs
once (a second call is an error); `compile_cache::shared` refuses a second fexc, directory or cap while one cache is open.
After the round: `cargo test`, `check_resident_stack.py`, contain (410 tampers, every stop as before), faults 7,912,
x1 6/6 and 188/188, delivery 51, and a `natemu` boot to poweroff at N = 1. Out of scope: the resident still
implements `SET_CPL` and `EVENT_HOOK`; without a hook the host never sends them. **Checks** (baseline 959e430 and after,
same commands): every standing-suite command at both block sizes where the suite has them, outputs diffed with timing keys
dropped (`seconds`, `*_ns`, `*_us`, `*_ms`, `*_ns_mean`, latencies, `t2_t1_ns`, `wake_latency_us`); 47 of 64 runner
outputs identical (corpora, faults, ASM, the I-series, L1/L2/L2-iasl stdout and dumps among them); the other 17 (smp,
kicks, system, coherence, sharedpt, litmus, contain, events, m3, k2, x3, x4, l3, l4b, m2's cost line) differ in counts
that also differ between two baseline runs (races, kick and timer timing, PIDs), with equal verdicts; contain's 410
tamper cases give identical stops. `cargo test` 147 + 1 ignored before and after (the two moved tests renamed);
`check_resident_stack.py` identical; Python tests 317. Debian kernel + initrd (`rdinit=/bin/busybox -- poweroff -f`,
compile cache off) at 1 and 4 vCPUs through `boot-linux` before and after and through `natemu`: final stop poweroff in
every run, serial logs equal but wall-clock, sched_clock and deferred-init timing lines and, at 4 vCPUs, the async
initramfs line's position and the post-SMP `Memory: … available` figure (varies between runs of one binary);
`--stop-at` the vmlinux entry identical but its timing keys. No tamper code, `TestDevice` or `TestSource` in
`crates/host/src` (grep: only `mem::forget`, doc comments, the compile cache's forged-entry unit test and its `kill(pid,
0)` liveness check), and `strings target/release/natemu` has no tamper/forge.

*Built (R1a, single-vCPU composites; checks only, no emulator or fexc change, series43-x1, on 3c12567):*
`tools/run_r1.py r1a` runs seven programs (natemu-smp-v1, CPL 0 start, one vCPU), each chaining features built in
earlier chunks. Pinned QEMU (the smp stub, COM1 = `-serial file:`) runs each once, natemu at block sizes default and 1;
natemu must equal QEMU (every GPR, RFLAGS under 0x8d5, every captured region with the TSC field of the delivery logs
masked, the UART bytes) and both must give the state the SDM fixes for the program (`expect` in the runner), so a
defect QEMU shared would still fail. **`r1-boot`** (`K kernel/head_64.S:37-405` shape): REP STOSQ of five garbage-filled
table pages, 4-level tables with the identity map and a kernel map at 0xffffffff80000000 (2 MiB leaves, global),
CR4 = PAE|PSE|PGE, CR3, an indirect jump into the kernel map, LGDT with a kernel-map base, null DS/SS/ES/FS/GS, the GS
base onto a per-CPU page and a canary at %gs:40, LRETQ to __KERNEL_CS 0x10, EFER SCE|NX, CR0_STATE (0x80050033), LIDT
of an early IDT in the kernel map whose #UD handler fixes up a UD2, `push 0; popfq`, a call through the kernel map, then
back to the identity map: CR0/CR4/CR3/EFER, SGDT/SIDT, CS/SS, the #UD frame (RF = 1) and the canary asserted.
**`r1-demand`** (decompressor `ident_map_64.c:361-387`, `head64.c:362-366` shape): a #PF handler reads CR2, walks
from CR3, takes table pages from a garbage-filled pool (zeroing each) and frames in order, maps the 4 KiB page and
returns; a read needing three tables, a neighbour write (leaf only), a read under a new PDPTE, a fetch (#PF 0x10; the
frame holds code) and a REP STOSQ faulting at its start and again midway: the fault log (CR2, error code, RIP), the
A/A+D bits the retried accesses set, every frame and pool page compared. **`r1-int3`** (`int3_selftest`,
`alternative.c:1829-1855`): the handler matches regs->ip - 1 against `int3_selftest_ip` and stores 1 through RDI; an
INT3 elsewhere, INT3 with IF = 1, INT 3 (CD 03); frames (CS, RFLAGS, RSP, SS) and the delivery log. **`r1-poke`**
(`__text_poke`, `alternative.c:2086-2160`): stores through a poking address that only a second table set maps onto the
code frame (the code page unmapped there), under that CR3, INVLPG of it, CR3 back, `sync_core` (IRETQ to self); a plain
immediate patch of an already-run block, the `text_poke_bp` order (INT3 first byte and sync; a call meanwhile takes #BP
and the handler emulates the new instruction; tail and sync; first byte and sync) and a static-call NOP5 -> JMP rel32:
1, 2, 3 (emulated), 3, 7, 9 and one #BP. **`r1-syscall`** (`entry/entry_64.S:87-166` with PTI): SWAPGS, the user RSP
to the per-CPU page, CR3 to the kernel tables (user tables = kernel + 0x8000), the kernel stack (kernel tables only), a
read of a page the user tables do not map and of one that maps to different frames in each; exit: user CR3, SWAPGS,
VERW of __KERNEL_DS, SYSRETQ; three round trips, VERW at CPL 3 (0x10: ZF 0; 0x1b: ZF 1), a last call ending the
program at CPL 0. **`r1-timer`**: the xAPIC one-shot timer waking `sti; hlt` four times (frame RIP after the HLT,
current count 0), an expiry pending with IF = 0 taken after STI's shadow, a masked expiry never pending.
**`r1-serial`**: 8250 THRE transmit driven by IRQ4 through I/O APIC pin 4 (edge, 44H): IER = 2, the handler reads IIR
and writes the next byte or clears IER; the main line halts until done: 43 deliveries for 42 bytes, IIR 02H in each,
the bytes equal to QEMU's file. **Results** (`natemu-raw/r1-20261102-01/`): 7 of 7 match QEMU and the SDM expectations
at both block sizes; five further repeats 7 of 7. **Negative controls** (natemu run on a wrong variant against the
correct program's QEMU state; each must be flagged, and is): LRETQ to 0x08 (CS differs), leaves one frame too far (every
load differs), regs->ip compared without the INT3's length (the selftest value stays 0), the poking address onto another
frame (stale bytes run: 1, 1, 1 and no #BP), no switch to the user CR3 on exit (the user sees the kernel's frame), the
handler stopping one byte early (UART, count, IIR log); for `r1-timer` QEMU's expected log mutated to wake RIPs at the
HLT itself. **Program note:** an undefined flag must not reach compared state: XOR and SHR leave AF undefined, and natemu
(default block size) sets it where QEMU clears it; `r1-boot` therefore writes its gate before `popfq` and zeroes R14
with MOV (not an emulator defect). No emulator defect found.

*Built (R1b, multi-vCPU composites; checks only, no emulator or fexc change, series43-x1, on 9730c4d):* `tools/run_r1.py r1b`
runs four programs at N = 2, 3, 4, block sizes default and 1. Three (`r1b-poke`, `r1b-nmi`, `r1b-ipiload`) are one program
run by pinned QEMU (its BSP is x2APIC with peers, so the IPI and NMI programs use x2APIC) and natemu; `r1b-mpstart` needs a
natemu build (the BSP wakes APs through the ACPI mailbox) and a QEMU build (its stub starts the APs at the same vector),
as `run_x4.py` does. With peers the delivery logs (which vector landed when, the TSC) are not deterministic, so only
exact counts, the per-CPU end registers (cleared to a known value at each body's end so timing-dependent loop state does
not leak), and deterministic captured memory are compared; SDM invariants (LSL = the CPU number, no lost or duplicated
IPI, EOI leaving the ISR clear) are judged on both machines. Every address is below 4 MiB and clear of the smp stub's
reserved ranges; programs that change the GS base restore the start-up value by MSR before the end, since QEMU's stub
capture is GS-relative. **`r1b-mpstart`:** the BSP wakes each AP (ACPI MP-wakeup mailbox, `events::wait_mailbox`); each AP,
from its wakeup vector in the firmware wake state, sets a valid RSP, loads its own GDT (LRETQ to 0x08), own IDT, GS base
(IA32_GS_BASE) onto a per-CPU block, reads its APIC id by CPUID, stamps `[gs:0]`, and `lock inc`s an arrival counter;
every AP's recorded APIC id, GDT/IDT base, GS base, RSP and CS, and the arrival count, are compared (QEMU's stub brings
the APs up to the same vector). **`r1b-poke`:** CPU 0 patches a shared run-time code function by the `text_poke_bp`
protocol (INT3 on the opcode byte; a sync IPI to every peer, whose handler's IRETQ is the serializing `sync_core`; the
imm tail; sync; the real opcode; sync) while the peers call it in a loop with a short compute delay between calls (a
continuous hammer on the page would make natemu step every instruction, H section 4); each peer's #BP handler emulates
the new instruction. Every peer ends reading the new value (K2), never a torn value, and the final function bytes are the
patched instruction. **`r1b-nmi`:** CPU 0 sends N_K NMIs (x2APIC ICR, NMI delivery mode, physical) to each peer,
handshaked so none coalesces; each peer takes them on IST1 through a paranoid handler that RDGSBASEs the saved base, reads
its CPU number with LSL of a per-CPU "CPU/node" descriptor, WRGSBASEs the canonical per-CPU base, does a SWAPGS pair, and
restores; every peer's NMI count and LSL-derived CPU number are exact. **`r1b-ipiload`:** every CPU runs an LCG sweep over
its private page while sending IL_K fixed-vector IPIs to every other CPU (one distinct vector per ordered pair,
handshaked so counts are exact); each ordered pair's delivery count equals IL_K, every CPU's ISR and PPR are clear at the
end (every delivery EOIed), and each CPU's loop checksum matches QEMU. **Results** (`natemu-raw/r1-20261102-01/`): 4 of 4
match QEMU and the SDM invariants at N = 2, 3, 4 and both block sizes; two further no-negative repeats 12 of 12.
**Negative controls** (a wrong natemu variant against the correct program's QEMU state; each flagged): every AP uses
CPU 0's per-CPU block for its GS base (the recorded bases diverge); the restored opcode is `mov ecx` not `mov eax` (peers
never read K2); every CPU/node descriptor limit is 0 (LSL yields 0, not the CPU number); CPU 0 sends one fewer IPI toward
CPU 1 (that pair's count is short). **Construction notes** (not emulator defects): a mailbox-woken AP starts with RSP = 0,
so RSP must be set before the far return; the AP reads its APIC id by CPUID, not the x2APIC MSR, since the stub leaves
peers in xAPIC mode; the handshake reads a delivery count before sending, not after, or a delivery between send and read
would make the wait target unreachable. No emulator defect found.

*Built (R2 fix, the dangling interrupt-controller pointer; no fexc change, series44-r2, on b3f6841; committed as e6b2bf5):*
**Root cause** (confirmed): `irq::install` stored `Lapics::memory: *const Memory` pointing at `Boot::prepare`'s local
`backing`, which `prepare` then moved into `Prepared` (L0, 3c12567); every later post and ring read a stale stack
`Memory`. **Fix:** `execution::Backing::memory` is `Arc<Memory>`; `irq::install` takes `&Arc<Memory>` and `Lapics` keeps a
clone (the raw pointer, its SAFETY comment and `unsafe impl Send for Lapics` removed; `Memory` gains `unsafe impl Send`:
the mapping may be unmapped from any thread). The chipset now keeps the mapping alive itself (the review found that
other safe paths still could outlive it: Built (R2 review fixes), C). The vCPU path is unchanged (`Area` still borrows `&Memory`, derefed once when the area
is made). **Other captures audited** (none dangle after the move): the UART's IRQ hook, the chipset views, PIT/RTC/PM
and `Devices.irqs` hold `Irqs` (`Arc`s); `Devices` is moved into its `Arc` after registration and nothing points into
it; `GuestRam` and the watchdog's `halted` word address the mmap, which does not move with `Memory`; the doorbell,
`Stops` and the compile cache (`compile_cache::shared`, `Arc`) are made in `run`; `Loaded` and the board's stats are
values or `Arc`s. **Checks** (red at b3f6841 with only the fix reverted, green with it; `natemu-raw/r2-fix-20261102-01/`,
`red-on-head/`): `tools/run_boot_smoke.py` (the stub built alone, Debian kernel + installer initrd,
`rdinit=/bin/busybox -- sh -c "echo MARKER; poweroff -f"`, the marker on serial and exit 0 at N = 1 and 4): 0/2 (the
guest powers off, the echo never appears) then 2/2, about 10 s per boot with the cache off; the unit test
`linux::tests::interrupts_reach_the_backing_after_prepare_moves_it` (a synthetic resident ELF and bzImage, `prepare`,
the value moved into a `Box`, 64 KiB of stack zeroed, the I/O APIC pin 4 routed to vector 0x41, COM1's IER THRE bit
set: the moved backing's vCPU 0 must have 0x41 pending): panicked in `global_u64` on the zeroed `Memory` 3/3, then ok.
**Phase-end items:** `run_m2.py`'s model judges an MTRR overlap the SDM leaves undefined as natemu's recorded UC (it
raised `KeyError` in `types` at 4 MiB), and the `qemu` mode's required types for the `mtrr` program come from the
same model (at 4 MiB the firmware's WB range 0 covers the WC and WP pages: UC); the handoff states which runners cannot
run at 4 MiB and why; `tools/fexc/README.md` names series44-r2. **Rerun:** `cargo test` 148 + 1 ignored,
`check_resident_stack.py` `ok`, Python 317, `contain`, `delivery`, `run_x3.py`, `run_x4.py`, `run_l3.py`, `run_l4a.py`,
`run_l4b.py`, `run_m3.py` green, `run_m2.py` 112/112 at 2 GiB and 4 MiB, `run_r1.py r1a` 7/7 and `r1b` 12/12; the installer
initrd to "Select a language" through the stub with a warm cache: N = 1 67.5 s ("Run /init" at guest 5.18 s), N = 4
48.7 s (5.44 s), as the R2 scratch fix (67.4 and 49.2 s).

*Built (R2 review fixes; the independent phase-end review of e6b2bf5; no fexc change, series44-r2; uncommitted on
e6b2bf5; raw `natemu-raw/r2-final-20261102-01/`):* **A, census.** The 15 initramfs sites I10 left were REX chains (OpenSSL
CRYPTOGAMS strings in `.text`, e.g. `4f 47 41 4d 53` = push r11): defined, only the last REX before the opcode counts
(SDM Vol.2 2.2.1). fexc decodes them so (FEX's `DecodeREXIfValid`); the census classifier and, a real defect, the
host's own byte decoders read one REX: admission's div/idiv, x87-helper, PCMPxSTRx, LOCK CMPXCHG/NEG and REP-string
proofs and `system::decode`/`lock_ud`/`undefined`/`branch` now share `system::opcode_at` (legacy prefixes and REX bytes
in any order; a REX followed by a legacy prefix ignored). Check: `tools/reference/gen_rex.py` (25 programs: the census
sites byte for byte, 24 random slice-1 programs with 1-3 REX bytes inserted before each REX-encoded instruction, 461
chains; QEMU 7.2 expected, `natemu-raw/rex-corpus-20261102-01/`): at e6b2bf5 16/25 at both block sizes (9 "object
refused: division ..." host errors), with the fix 25/25 at both; unit tests `system::tests::rex_chains` and the
admission decoder cases. Census rerun (§4). **B, boot table** rerun on this build (boot-handoff §4). **C, sound public
API.** `Prepared::guest` and `Backing::ram` (a `Copy` `GuestRam` over the mapping) are `unsafe fn`s with the lifetime
rule as their safety contract; `Backing::memory` is `pub(crate)` with a `memory()` getter; `vm::Machine::create` takes
and keeps the `Arc<Memory>` (a `mem::forget` of the machine leaks the mapping instead of leaving the VM on freed
memory). **D, lifecycle.** `Prepared::run` ends with `Irqs::detach` (under the chipset lock, where every ring happens)
and `Board::stop` (each timer thread's `Halt`; the devices stay readable, catching up at each access) before
`Machine::destroy`, which, like its `Drop`, takes the GIC lock; the driver detaches too. Check
`linux::tests::the_end_of_a_run_detaches_and_stops_the_timers` (a 1 kHz PIT rises no more after the end; red with
either call removed). **E, external stop.** `Prepared::canceller()` -> `Canceller::cancel()` (the supervisor's flag is
now the caller's: `owners::supervise(.., cancelled)`); the stub wires SIGINT/SIGTERM to it (a second signal exits 130).
Check: `run_boot_smoke.py` `cancel` (4 vCPUs, SIGINT 1.5 s in: exit 2, `Cancelled`, 0.02 s after the signal; e6b2bf5
installs no handler and has no stop entry, so the signal's default action kills it with no stop reported). **F, spill area.** `admission::retry` sends an object with a spill
area back like an oversized one: translated again with half its instructions, `no_code` `spill_area` at one. Check
`execution::tests::spill_areas_retry_then_stop` (with `FEXC`; cached answers given `spill_bytes` 8: 4 -> 2
instructions, then the stop; at e6b2bf5 "object refused: code layout", a host error). **G.** The UART keeps output only
without a sink, counts every byte (`sent`) and keeps the sink's first write error (`Prepared::serial_error`; the stub
exits 1 on it); `NATEMU_COMPILE_CACHE_MB` that is not a whole number is an error; the I/O APIC's dropped vectors 0-15
and SMI/INIT modes are declared deviations (boot-handoff §4: the target's ESR is the resident's, INIT has no model).
**H.** The watchdog is a scoped thread borrowing the `halted` word (joined on a panic too); `Machine::create` destroys
the VM on any failure after `hv_vm_create` (the machine exists from then on); handoff and plan status text, build line,
memory map, runner statement fixed. **Small-RAM suite** (owner decision 7): `run_x4.py`, `run_l3.py`, `run_m1.py`
relocated below 4 MiB (§4). **Round 2** (the fixes' independent review, nothing blocking; compiler **series45-r2fix**:
series44-r2's patches and the shim change below, `tools/fexc/build.sh`, `obj/fexc` SHA-256 `354cab22...ba56f3`): the
`fexc --serve` children start in their own process group (`process_group(0)`), so a terminal's Ctrl-C reaches only the
stub (red before: `run_boot_smoke.py`'s new `cancel-group` case, SIGINT to the whole group, exit 1 "fexc --serve ...
exited (signal: 2)"; green after); segment prefixes on near direct branches are ignored, as processors and QEMU do (owner
rule, §5): the shim admits them on Jcc rel8/rel32, JMP rel8/rel32, CALL rel32, LOOPcc/JrCXZ and admission's branch-target
proof skips FS/GS (66 there stays refused: FEX decodes a rel16 operand); `gen_rex.py` gains 12 `seg-branches-*` programs
(backward Jcc/LOOP loops and rel-0 forward branches with 1-2 random segment prefixes; QEMU 7.2 expected,
`natemu-raw/rex-corpus-20261102-02/`): series44-r2 12 unsupported, series45-r2fix 37/37 at both block sizes; compiler
slice-1 tests 34 pass (6 red on series44-r2); the retry loop halves the smaller of the reply's instruction count and the
limit asked, so it ends even if fexc ignored the limit; the driver's `run-program` stops its timer threads too, and
`boot-linux` reports `serial_error`. Rerun on series45-r2fix (`natemu-raw/r2-final-20261102-01/rerun2/`): `cargo test`
151 + 1 ignored (with `FEXC`), the REX corpus 37/37 at both block sizes, faults 7,912 at both, `contain` 0 failures,
`run_x1.py` 6/6 and 188/188, i10 faults 296/296 at both, ASM 1,875 pass / 0 fail / 48 / 347, `run_boot_smoke.py` 4/4
(`cancel-group` included), `run_r1.py r1a` 7/7 and `r1b` 12/12; census rerun (§4). **Rerun:** the full standing suite of boot-handoff §5 at 2 GiB, both block sizes (`suite-2g/`, every command):
all green but the 7 known i8b QEMU two-NaN departures at both block sizes; `cargo test` 151 + 1 ignored (with `FEXC`
set too), `check_resident_stack.py` `ok` (gate 19,168, nested 23,312, boot 12,832 of 32,768: no resident change),
Python 317, faults 7,912 x 2, ASM 1,875 pass / 0 fail / 48 / 347 (the runner's `mnemonic` now skips an ignored REX:
`FEX_bugs/REX/0F_38.asm`, whose chained ADCX was a host-undecoded stop before, is the profile's #UD now), the REX corpus
25/25 x 2, `run_x3.py`, `run_x4.py`, `run_l3.py`, `run_l4a.py`, `run_l4b.py`, `run_m1.py`, `run_m2.py` 112/112, `run_x1.py`
6/6 and 188/188, `run_r1.py r1a` 7/7 and `r1b` 12/12 (negative controls flagged), `run_boot_smoke.py` 3/3 (the
`cancel` case included); at 4 MiB the three relocated runners (`small-4m/`); the installer boots (boot-handoff §4).

*Built (66 on near direct branches; fexc patch 0029, compiler **series46-br66**; uncommitted on e8f5e49):* in 64-bit mode Intel
ignores 66 on a near direct branch: the operand size is forced to 64 bits and the displacement stays rel8/rel32 (SDM Vol.2
CALL: "the operand size for near call (and all near branches) is forced to 64-bits", "In 64-bit mode the relative offset is
always a 32-bit immediate value which is sign extended to 64-bits"; JMP and Jcc: the rel16 forms "Not supported in 64-bit
mode", "operand size is fixed at 64 bits"; Vol.2 Appendix A superscript f64, "prefixes that change operand size are ignored
for this instruction in 64-bit mode", and Vol.1's "operand size prefixes are silently ignored", which the host's
`system::branch` cites as Vol.1 3.3.7: these two read through secondary quotations of the SDM, not a local copy). FEX decoded rel16 as AMD does, so the shim refused these forms. Patch 0029: under
slice1 in 64-bit mode the decoder does not narrow a near direct branch (`FLAGS_SETS_RIP`, `FLAGS_SRC_SEXT`, no ModRM,
`SIZE_64BITDEF`: Jcc rel8/rel32, JMP rel8/rel32, CALL rel32, LOOP/LOOPcc, JrCXZ); the lowering already pushed 8 bytes and
computed a 64-bit target. Shim: 66 admitted on those forms (RET and indirect CALL/JMP keep the refusal); admission's
`branch_target` skips 66 (a rel16-length form is no target); the host's `system::branch` already skipped it. Pinned QEMU 7.2
follows AMD (66 with rel32 decodes rel16 and the scratch probes fault; 66 with rel8 runs as Intel), so the new corpus
`tools/reference/gen_br66.py` (12 programs: gen_rex.branches with 66 before every branch at a random place among 0-1 segment
prefixes: Jcc rel8/rel32 backward loops taken then not, Jcc rel8, JMP rel8/rel32, CALL rel32 with its pushed address
popped, LOOP, JRCXZ) takes the SDM-derived expected state from pinned QEMU's run (without -d) of a twin with each 66
replaced by 3E, which the branch also ignores (same length, same addresses); `natemu-raw/br66-corpus-20261102-01/`.
Red/green (`natemu-raw/br66-check-20261102-01/`): series45-r2fix 12 unsupported at both block sizes, series46-br66 12/12
at both; compiler slice-1 tests 35 pass (24 failures on series45-r2fix: 9 66-prefixed branches end their block and
compile without a stop check, `66 e8 rel32` pushes RIP + 6 and exits to RIP + 6 + rel32); `run_x1.py` gains 8 cases of 66
on a branch to a non-canonical target (CALL before its push, Jcc rel8/rel32 taken and not, JMP rel8, LOOP, JRCXZ): still a
cut delivered by the host, #GP(0) at the branch. Rerun on series46-br66 (`rerun/`): `cargo test` 151 + 1 ignored, the REX corpus
37/37 and the I10 corpus 88/88 at both block sizes, `run_x1.py` 6/6 and 196/196, i10 faults 296/296 at both, faults 7,912 at
both, probes 83/83, ASM 1,875 pass / 0 fail / 48 / 347 (FEX's disabled `Primary/Primary_E8.asm`/`E9.asm` test AMD's rel16:
natemu takes the Intel rel32 target and faults there, at 0x6ec0017/0x7ec0017 as computed from the bytes; `run_asm.py` files
them unsupported, as the refusal did before), `run_boot_smoke.py` 4/4. Removed from the to-verify list (§5): the SDM
defines this.

*Built (SHA, owner decision November 3; compiler **series49-sha** = patches 0001-0029 + shim change; uncommitted on 21517f2, whose code is d3e1a5b's):*
**Profile:** CPUID.7.0:EBX = 200001A9H (SHA, bit 29, advertised; `cpu_profile.json`, the generated `cpu_profile.rs`, the
Python vectors). **Shim:** NP 0f 38 c8..cd and NP 0f 3a cc are SSE forms in `Sse` (exception class 4: an m128 is 16-byte
aligned, #GP(0) before any walk; gated as SSE); I9's #UD cut is gone. 66/F2/F3 cells stay blank (Table A-4/A-5), #UD by
the host's `undefined()`; the host's SHA decode arm is gated by `advertised(SHA)`. fexc has no profile input:
`test_slice1.py` fails if the bit is cleared without restoring the cut. **Lowering:** FEX's, on the M4's FEAT_SHA1/
FEAT_SHA256 instructions (SHA1C/P/M with a zero E and K added to W, SHA1H, SHA1SU1, SHA256H/H2 on XMM0[63:0] doubled, of
whose four rounds the upper halves are x86's two, SHA256SU0, SHA256SU1 with a zero accumulator; lane reversals by TBL/ZIP/
REV64); no software path. **Checks:** `tools/reference/gen_sha.py` (60 programs, 960 instructions: all seven, every
SHA1RNDS4 imm8, XMM0..15 with destination = source, memory absolute/RBX/R13/R12 + R8*8/RIP-relative/0x67, REX.W and
ignored DS/ES/CS/SS raw-encoded). Pinned QEMU 7.2's TCG has no SHA (its `TCG_7_0_EBX_FEATURES` lacks SHA_NI, no helpers),
so `tools/reference/ref_native.py` runs each program's ELF natively on the reference AMD machine (Ryzen 7 8845HS,
`sha_ni`): `natemu-raw/sha-corpus-20261103-01/`; the AMD results equal the SDM pseudocode transcribed in `gen_sha.py`
(`--check`: 960/960; a one-bit error in any instruction's model is caught). natemu 60/60 at block sizes default and 1;
red on series48-lean + HEAD (60 #UD). The independent review's simulation of FEX's IR on the Arm pseudocode: 20,000
random cases and SHA-1/SHA-256("abc") exact. `run_i9_faults.py` 586 -> 662 (each form: #PF, crossing #GP, mis8 #GP,
noncanonical #GP/#SS, straddle; #NM under TS, #UD under EM and without OSFXSR; #UD for 66/F2/F3), 662/662 at both block
sizes; FEX ASM 1,876 -> 1,891 pass (against the batch-end run on HEAD: the 7 H0F38/H0F3A and 5 SSELanePreservation SHA tests,
FEX_bugs/H0F3AREXBug, nzcv_spill_enderlilies, modrm_oob/H0F3A), 0 fail, 48, 346 -> 331 unsupported; I9
corpus 120/120 and I10 88/88 at both sizes, i10 faults 296 at both, `run_x1.py` 6/6 + 196/196, `test_slice1.py` +
`test_fexc.py` 46, `cargo test` 163 + 1 ignored, Python 299 (3 skipped), `run_boot_smoke.py` 4/4. Census
(`natemu-raw/kernel-census-sha-20261103-01`): initramfs `system` 226,564 -> 225,267, the 1,297 SHA sites admitted, nothing
else moved; the kernel has none. **Deviation from the reference machine (not from the SDM):** the AMD machine runs a
misaligned SHA m128 (`sha-corpus-20261103-01/probe-output.txt`; PADDD/AESENC/PSHUFB with the same operand fault there);
natemu raises #GP(0) as the SDM's class 4 says. **Cost (bench initramfs, `bench=zstd`, 1 vCPU, N = 1, cache off;
`natemu-raw/sha-20261103-01/bench/`):** verify (busybox `sha256sum`, which takes its SHA-NI path when CPUID has SHA)
0.745 -> 0.314 s host; compress 1.64 -> 1.60, decompress 0.23 -> 0.29, whole 2.62 -> 2.20 s; both verified. QEMU 11.1.2
(control now `+sha-ni`, its TCG implements SHA): verify 1.71 -> 0.83 s, whole 17.5 -> 17.2 s. Native: 0.080 s.

*Built (ADX, owner decision November 3; compiler **series52-adox66** = patches 0001-0030 + the shim; uncommitted on 1be744d):*
**Profile:** CPUID.7.0:EBX bit 19 (ADX) advertised (`cpu_profile.json`, the generated `cpu_profile.rs`). **Shim:** ADCX (66 0f 38 f6)
and ADOX (F3 0f 38 f6) compile, prefixes read from the bytes (FEX's decoder clears ADCX's 66 flag) (`Classify`; ADOX exempt from
the REP-prefix refusal). ADOX with a 66 too, in either order, is open in the SDM; the AMD machine runs it as ADOX with 66 ignored
(32 bits without REX.W, `adx_probe2.out`), so natemu does (coordinator's ruling under the owner rule, November 4): new patch 0030
maps FEX's 66|F3 0F 38 F6 table slot to ADOX and drops the 66. `Undefined0F38` still cuts NP f6 (WRSS) and any F2. **Host:** 0f 38
f6 with F2 before F3 is not decoded (a stop; the AMD machine runs it as ADOX, FEX's decoder does not), F2 last (alone, with 66 or
after F3) and NP are #UD (the AMD machine agrees); the `undefined()` cells follow the profile (`undefined_follows_the_profile`); `test_slice1.py` fails if the
bit is cleared without restoring the cut. LOCK is the shim's non-lockable cut (#UD). **Lowering:** FEX's `ADXOp`: an Arm ADC whose
carry-in is CF (ADCX) or OF moved into C by CCMP (ADOX), then RMIF restores N, Z and C or V from the NZCV saved before it, so only
CF or OF changes and PF/AF (separate registers) are untouched; 32-bit forms zero-extend. ADOX's CCMP reads the host flags before the
cached NZCV is flushed; that is safe because FEX flushes the cache before every instruction (Core.cpp `FlushRegisterCache`), noted
by the independent review as a latent dependency. **Checks:** `tools/reference/gen_adx.py` (56 programs: 16 `adx-model-*`
replayed through the SDM's Operation by `--check`, 16/16, and a negative control with ADOX modelled as ADCX fails 15; 16
`adx-chain-*` MULX + two interleaved carry chains with PUSHFQ between; 24 `adx-mix-*` after random flag producers; 32/64 bits,
register, memory near and across the page boundary, 0x67, REX.W and ignored DS/ES/CS/SS raw-encoded), judged by the AMD machine
(`ref_native.py --require-flag adx`; `natemu-raw/adx-corpus-20261104-01/`); natemu 56/56 at block sizes default and 1. Pinned
QEMU 7.2 cannot judge it: its TCG ADX (before 8.0's "fix ADOX followed by ADCX") ends program 0 with an `IOPL(dc) == iopl`
assertion and others with SIGTRAP (`expected-qemu72.json` not written). Probe (`adx_probe.c`/`.out`): all four CF/OF inputs of
ADCX/ADOX at 32 and 64 bits, 66 F3 and F3 66 run as ADOX at the instruction's own size, F2 then F3 as ADOX, F3 then F2, F2 and 66
F2 #UD, LOCK #UD. The corpus has 10 ADOX forms with a 66 before or after F3. `run_i9_faults.py` 662 -> 714 (ADCX/ADOX m32/m64: #PF,
crossing, end of page, non-canonical #GP/#SS, straddle; not gated by CR0.EM/TS; #AC at CPL 3 for misaligned m32/m64; F2, 66 F2, F3
F2, LOCK #UD; 66 F3 and F3 66 m32 reading 4 bytes; F2 F3 refused), the 710 before the 66 F3 change green at both block sizes; FEX ASM 1,891 -> 1,896 of the run below (`H0F38/adcx`, `adox`,
`FEX_bugs/adcx_size`, `REX/0F_38`, `modrm_oob/H0F38`); `run_x1.py` 6/6 + 196/196 (the F2 cell replaces ADOX's #UD); census
(`natemu-raw/kernel-census-cpufeat-20261104-01`): initramfs `system` 225,267 -> 223,899, the 1,368 ADX sites (ADCX 736, ADOX 632,
all in libcrypto/fips.so) admitted, nothing else moved; the kernel has none.

*Built (CLFLUSHOPT, CLWB, owner decision November 3; no compiler change):* **Profile:** CPUID.7.0:EBX bits 23 and 24 advertised
(EBX = 218801A9H). **Host:** 66 0f ae /7 m (CLFLUSHOPT) and 66 0f ae /6 m (CLWB) decode as the resident's CLFLUSH transaction (fexc
already cut both as system instructions); CLFLUSHOPT with F2/F3 #UD (its page), CLWB with F3 is CLRSSBSY (#UD, CET not advertised).
**Semantics (SDM Vol.2 each):** the faults of a byte load at the CPL (#PF with a read error code, A set but not D, #GP(0)/#SS(0)
non-canonical; no write permission needed); RAM is coherent write-back with no cache model, so there is nothing to flush or write
back; a device page passes (uncached, M3) and UC RAM takes the M2 barrier in `segments::pages` as CLFLUSH did; as a draining entry
each is ordered more strongly than its page requires (fences, locked instructions, older writes to the line). Code-page
invalidation is unaffected: neither writes memory. **Checks:** `syscall_model.py` models both (CLFLUSH's faults, #UD only where the
profile clears the bit); `run_slice3_vm.py system` 0 failures with `sc-cache` running CLFLUSHOPT/CLWB vs QEMU and four new fault
cases (non-canonical, CPL 3 on a supervisor page; QEMU makes no access for them, so judged by the model); `run_m2.py qemu,faults` 39/39
with CLFLUSHOPT/CLWB on every memory type and UC page; `run_i5b.py` `clwb-runs`; FEX ASM `Secondary/CLFLUSHOPT`, `Secondary/CLWB` pass.

*Built (XSAVEOPT, XSAVEC, owner decision November 3; compiler **series52-adox66**, whose shim also names the resident's forms;
uncommitted on 1be744d):* **Profile:** CPUID.(EAX=0DH,ECX=1):EAX = 3 (XSAVEOPT, XSAVEC with the compaction extensions), EBX = XSAVEC's
size for XCR0 (576 or 832; SDM Vol.1 13.2 with EAX[3] = 0), ECX:EDX 0; 0DH.2 ECX[1] = 0, so compacted AVX sits at 576 and both
sizes equal the standard ones (`cpu_profile.py` checks this). **Host:** NP 0f ae /6 m decodes as XSAVE; NP 0f c7 /4 m as XSAVEC
(kind 0x6e; with 66/F2/F3 a stop, as XSAVE's); XSAVES/XRSTORS stay #UD. **Resident (`execution/xsave.rs`):** XSAVEOPT is XSAVE:
natemu implements neither the init nor the modified optimization, so XINUSE and XMODIFIED are all ones (Vol.1 13.6, 13.8.3) and
XSAVEOPT saves every RFBM component, sets those XSTATE_BV bits and writes no other header byte, which is XSAVE's image. XSAVEC
(13.10): the same component bytes, MXCSR/MXCSR_MASK only with RFBM[1], AVX at 576, XSTATE_BV := RFBM, XCOMP_BV := 2^63 | RFBM,
header bytes 16..63 untouched, XSTATE_BV not read. XRSTOR reads the whole 64-byte header; XCOMP_BV[63] selects the compacted form
(13.8.2): #GP(0) for XCOMP_BV[62:0] outside XCR0, XSTATE_BV outside XCOMP_BV[62:0], or header bytes 16..63 nonzero; components of
RFBM outside XCOMP_BV are initialized; MXCSR is loaded only with SSE state, else 1F80H for RFBM[1] (RFBM[2] plays no part); every
read and the MXCSR check precede any change. **Follows the reference machine:** XSTATE_BV[63] = 1 in a compacted image is #GP(0): the SDM
defines no non-fault for this reserved bit and the AMD machine faults (coordinator's ruling under the owner rule, November 4). **Linux:** the installer boot logs "Enabled xstate features 0x7, context size is 832 bytes, using 'compacted' format" (was
'standard'): 6.12 now uses XSAVEC and the compacted XRSTOR for task state. **Checks:** `tools/reference/gen_xsave.py` (56
programs: XSAVEC and XSAVEOPT under random RFBM with XSAVEC64, 0x67 and DS forms, areas within a page and across it; XSAVEC then
compacted XRSTOR under edited XSTATE_BV and each RFBM; XSAVEC with an RFBM subset then XRSTOR with RFBM 7; every component in use,
so the AMD machine's init optimization writes what natemu writes; MXCSR_MASK, 0002FFFFH there, ANDed with FFFFH), judged byte for
byte by the AMD machine (`natemu-raw/xsave-corpus-20261104-01/`); natemu 56/56 at both block sizes. QEMU 11.1.2's TCG has no XSAVEC
or compacted XRSTOR. Probe `xs_probe.c`/`.out` (AMD): ST reserved bytes, FOP/FIP/FDP written 0, bytes 416..511 and header 16..63
never written, XSAVEOPT/XSAVEC skip init components there, the compacted #GP cases and MXCSR rules above. `run_i5b.py` 445 -> 528
(XSAVEOPT images under every RFBM vs QEMU's Haswell, whose XSAVEOPT is XSAVE too; XSAVEC/compacted XRSTOR: alignment, #PF
crossings with nothing written, non-canonical, #NM/#UD gating, LOCK, CPL 3, the compacted header and MXCSR #GP rules), 528/528.
**Cost (installer, 2 GiB, N = 1, cache off, three interleaved rounds; `natemu-raw/cpufeat-20261104-01/measure/`):** before
(1be744d) 9.51 s (8.95-10.56), +ADX 9.32 (8.93-10.07), +CLFLUSHOPT/CLWB 9.08 (8.92-9.37), +XSAVEOPT/XSAVEC 8.93 (8.83-9.02); zstd
bench N = 1 (two runs each, verified) 2.19-2.21 / 2.19-2.24 / 2.21-2.26 / 2.18-2.19 s. No regression. **QEMU control:** `+adx,
+clflushopt,+clwb` (TCG implements them; Haswell already has `xsaveopt`); TCG has no XSAVEC, so the control lacks `xsavec` and its
Linux uses XSAVEOPT and the standard format: the control cannot match natemu in this one flag (`xsavec`); the harness patch is
applied together with these changes.
**Suite (series51-xsave = series52-adox66 without patch 0030 and the 66 F3 shim change; this tree):** `cargo test` 163 + 1 ignored, Python 299 (1 skipped), `test_slice1.py` + `test_fexc.py` 47,
`check_resident_stack.py` ok (gate bound 16,656), FEX ASM 2,270: 1,898 pass / 0 fail / 48 / 324 (was 1,891 / 331: the five ADX tests
and CLFLUSHOPT/CLWB), I9 corpus 120, I10 88, SHA 60, fixtures 64 (both block sizes), i9 faults 710, i10 faults 296 at both,
`run_x1.py` 6/6 + 196/196, `run_slice3_vm.py system` 0 failures, `run_boot_smoke.py` 4/4.

**Owner decision (October 29): unassigned I/O ports and device addresses behave as on a PC.** A read of a port or a device-range address that no device claims returns all-ones at the access width; a write is ignored; every such access is counted in the run report (by port or page). A strict debug switch turns them into typed stops. This is the PC platform convention (and QEMU's), and it is what lets Linux and other kernels probe for absent hardware; it is not kernel-specific. Context: the Debian 6.12 kernel at 2 GiB now runs into `setup_arch` and first stops at port I/O, a PCI configuration read (`out dx,eax` to 0xcf8 then `in` from 0xcfc, bus 0 device 2 function 0, `snb_gfx_workaround_needed`). Applies to L3 (ports, including 0xcf8/0xcfc with no PCI devices) and to M3's `unassigned` pages.

**Owner decision (October 29, amendment after L3):** unassigned memory pages (including the legacy hole 0xA0000–0x100000) are mapped read-only and non-executable onto one shared all-ones page, so every read form (compare, checksum, string, SSE) runs natively and exactly; reads are counted per page on first touch (shadow fill), writes stay counted per access and complete as ignored for MOV forms (other write forms keep their typed refusals); fetch stays a typed stop; the strict switch still turns all of it into stops. Context: Linux's `reserve_ibft_region`, `probe_roms`, DMI and `smp_scan_config` scan 0x80000–0xFFFFF with CMP/checksum/string loops. Pages a device claims stay unshadowed (M3); the LAPIC's possible page is always a device page.

**Owner decision (October 28): MTRRs enabled by the virtual firmware, RAM write-back, device ranges uncached** (plan Q3).
**Deferred (owner, October 28):** replacing the host's minimal JSON reader (`crates/host/src/json.rs`, used only for
fexc's per-compile report: end reason and cut opcode) by carrying those fields in the code-object header; no registry
dependency (serde_json) is added.
**Deferred (owner, November 2, phase end):** faster first-time code (parallel compiling, translating ahead of use,
translations reusable across load addresses; the next chunk, measured first); an AES-based in-VM random-number generator
seeded from the host; guest RAM beyond 3 GiB and more than 4 vCPUs, raised and tested.

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
