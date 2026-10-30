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
| Guest RAM and layout | **Decided:** M69 map, default L = 2 GiB (configurable 1–3 GiB), H = 0; tests choose their own size | Built (M1): guest RAM a run-time size R (2 MiB multiples to 3 GiB, default 2 GiB), per-page structures sized for 3 GiB and scanned by use, [R, 4 GiB) non-RAM, shadow pool 288 pages, resets counted; the legacy hole non-RAM since L3 (the unassigned rule) | Standing suite at 4 MiB and 2 GiB; programs above 16 MiB and at holes | M1 |
| Memory types: PAT, PWT/PCD, MTRR | Linux PAT = WB, WC, UC-, UC, WB, WP, UC-, WT (`K mm/pat/memtype.c:344`, written `:234`); LAPIC/IOAPIC mapped UC | Walker evaluates PAT/PWT/PCD (and MTRR if advertised) instead of halting (`shadow status=1 detail=7`, slice-3 S3a2); RAM of any type runs as coherent WB (declared deviation); device pages take M3 | Paging cells with PAT bits (`coherence_model.py` profile updated); QEMU | M2 |
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
accesses from EL1, removing the guard for scalar accesses) are owner items for the deferred list (Q5).

**Remaining design choice (X2/L5):** how a pending interrupt reaches a vCPU running linked code — the existing precise
stop (host kick via `hv_vm_protect`, slice-3 §1) or an in-VM native timer for local LAPIC deadlines. X2 builds on the
precise stop; L5 measures whether timer kicks need the second route.

**Decision point P1 (performance diagnosis, reported; independent replication pending; `experiments/track03/perf-diagnosis/`).**
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

*Built (I1, G1 minimum):* the guest-state page is the vCPU's existing FEX frame page with a second alias: EL0 read/write
at `PRIVATE + 0x24000` (`execution::GUEST_STATE_VA`, x28 at every block entry), while the EL1-only alias stays at
`FRAME_VA` (`PRIVATE + 0x23000`, the resident's view; the E3 window probes of that page still give #GP). It is inside the
window, as the owner's one page per vCPU at a fixed non-canonical address; `aborted` needs no exemption because the page
is mapped, so an access to it never aborts and `private_access` cannot misfire. Extent: exactly one 4 KiB page per vCPU;
an access straddling into it from `PRIVATE + 0x23000` or out of it into `PRIVATE + 0x25000` faults (#GP from the site),
and so does every other window page (`run_slice1_faults.py` window class `guest-state` and its straddles). Deviation: a
guest access inside the page reaches the vCPU's own guest state instead of #GP. What the resident trusts: it reads back
only DF (FEX's byte at `frame::DF`, banked in `bank`); TF, IF, IOPL, NT, AC, VIF, VIP and ID bytes (RF, VM as 0) are
rewritten from the bank at every block entry (`frame::flag_bytes`), so a guest write to them lasts at most to the next
entry and only `pushf` sees it; the FS/GS bases (`frame::FS_BASE`/`GS_BASE`, I7) are likewise written from the bank at
every `enter` and never read back. Admission lets x28 load the flag bytes and store only DF. Invariants: the abi tests
`guest_state_page_layout` (fields inside the page, `pushf` image, DF round trip) and the per-run frame check in
`drivers/program.rs` (`frame_intact`: any nonzero byte outside `frame::GUEST_STATE` fails the run). Open (owner): check
which kernel paths may be handed arbitrary addresses that could land in `PRIVATE + 0x24000..0x25000` (user-supplied
pointers before access_ok, probing helpers), where x86 would raise #GP.
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
DE, PCE, OSFXSR, OSXMMEXCPT, FSGSBASE, OSXSAVE stay refused. IA32_APIC_BASE: the BSP flag (bit 8) is R/W (SDM Vol.4
Table 2-2; the profile's mask was corrected after review, so the BSP may write back what it reads); a write changing
EN, EXTD or the base is refused (no LAPIC yet; the profile notes the x2APIC -> xAPIC and disabled -> x2APIC #GP rules
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
prefixes in 64-bit mode, so the default segment decides, while Intel Vol. 1 §3.3.7.1 suggests #SS for an SS override). Checks: `tools/reference/gen_i7.py` corpus (67
programs, bases near, far, negative and 0; QEMU sets them with WRFSBASE/WRGSBASE) 67/67 at CPL 3 and 0, block sizes
default and 1; faults: 252 `segment_probes` and 979 page-mutation cases on that corpus (#PF at base + EA), all match;
`verify_precision.py` matrix and adversarial programs with FS/GS forms clean; on the corpus it finds one defect that is
I1's, not I7's (also on series14 with the prefixes removed): after a 16-bit register `cmpxchg` following BMI/POPCNT forms,
FEX leaves AF's x27 write out, so a fault at a later access in the block would bank a stale AF (P6, program
`i7-random-45`). Census FS/GS class: kernel 36,958 → 0,
initramfs 27,599 → 2. FEX ASM suite unchanged (its FS/GS tests need WRFSBASE/WRGSBASE or SSE).
*Revised (October 29, fexc series27; coordinator's decision): CS/DS/ES/SS prefixes follow Intel.* The presented CPU is
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
invalid). Choices: **AM/AC** — alignment checking is not built; the state CR0.AM ∧ RFLAGS.AC ∧ CPL 3 is refused as a mode
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
— recorded with no memory-type effect (RAM is write-back; M2 owns memory types). Not built: CLTS (`0f 06`, no use in the
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
FS/GS on STOS/SCAS (hardware ignores it: ES:RDI takes no override); FS/GS (alone) and CS/DS/ES are
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
error code; the walk sets A) and no other effect; on a page that is not write-back RAM (e.g. a UC device mapping) it stops
`unsupported_system` today, as every resident operand access does (M2/M3 own memory types and device pages). **#UD** for forms the profile does not advertise or that need VMX/SVM/
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
device page (device ranges are UC; RAM with a non-WB type still stops, M2's). **Fault priority:** at the abort the walk of
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
**I5c (recorded, not built; the I5a review's design): unmasked SIMD exceptions.** Hardware FP traps are not used although
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
(#MF: the I5c-style design applies, blocks keyed by FCW's masks with per-instruction FPSR checks; FLDCW/FLDENV/FRSTOR refuse
them today, 160 + 44 + 0 initramfs sites take the transaction); FIP/FDP (0 in images); FLD/FSTP m80 as resident round
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
0x600000), `--vcpus N` its processor count (1..4 with `--load-only`, 1 for a run until X4). **Sources:** ACPI 6.5
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

**Owner decision (October 29): unassigned I/O ports and device addresses behave as on a PC.** A read of a port or a device-range address that no device claims returns all-ones at the access width; a write is ignored; every such access is counted in the run report (by port or page). A strict debug switch turns them into typed stops. This is the PC platform convention (and QEMU's), and it is what lets Linux and other kernels probe for absent hardware; it is not kernel-specific. Context: the Debian 6.12 kernel at 2 GiB now runs into `setup_arch` and first stops at port I/O, a PCI configuration read (`out dx,eax` to 0xcf8 then `in` from 0xcfc, bus 0 device 2 function 0, `snb_gfx_workaround_needed`). Applies to L3 (ports, including 0xcf8/0xcfc with no PCI devices) and to M3's `unassigned` pages.

**Owner decision (October 29, amendment after L3):** unassigned memory pages (including the legacy hole 0xA0000–0x100000) are mapped read-only and non-executable onto one shared all-ones page, so every read form (compare, checksum, string, SSE) runs natively and exactly; reads are counted per page on first touch (shadow fill), writes stay counted per access and complete as ignored for MOV forms (other write forms keep their typed refusals); fetch stays a typed stop; the strict switch still turns all of it into stops. Context: Linux's `reserve_ibft_region`, `probe_roms`, DMI and `smp_scan_config` scan 0x80000–0xFFFFF with CMP/checksum/string loops. Pages a device claims stay unshadowed (M3); the LAPIC's possible page is always a device page.

**Owner decision (October 28): MTRRs enabled by the virtual firmware, RAM write-back, device ranges uncached** (plan Q3).
**Deferred (owner, October 28):** replacing the host's minimal JSON reader (`crates/host/src/json.rs`, used only for
fexc's per-compile report: end reason and cut opcode) by carrying those fields in the code-object header; no registry
dependency (serde_json) is added.

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
