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
