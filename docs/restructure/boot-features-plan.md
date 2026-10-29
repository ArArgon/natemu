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
