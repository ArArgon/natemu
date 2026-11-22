# The boot-time interpreter's real-code suites: kvm-unit-tests and OVMF

Status (November 22, 2026, branch `boot-interp`): the inputs are built by the framework from pinned upstream sources,
both run on the references (the pinned QEMU 11.1.2 under TCG, and real hardware through KVM on the x86 reference
machine) and their answers are locked. kvm-unit-tests runs in natemu (case `kut-natemu`); OVMF runs in the firmware boot mode ([firmware-mode.md](firmware-mode.md),
case `ovmf-natemu`): `main32` and `reset` to SEC's entry with KVM's registers (`reset` through its MOV CR4 640H: natemu
accepts CR4.MCE without presenting MCE, a recorded deviation), of the [boot-time interpreter plan](m2-boot-interpreter-plan.md). This page is the contract those
cases build to.

## The pieces

| File | What it is |
| --- | --- |
| `tools/boot/kvm-unit-tests/` | The recipe: `container.sh` (upstream's `./configure --arch=x86_64; make`), `debs.sha256` (gcc 14, binutils, make). |
| `tools/boot/ovmf/` | The recipe: `container.sh` (`make -C BaseTools`, `build ... OvmfPkgX64.dsc`), `debs.sha256` (gcc 14, g++, nasm, iasl, uuid-dev, python3). |
| `tests/resources.toml` | The sources: `kvm-unit-tests-src`, `edk2-src` and the five edk2 submodules the build reads. |
| `tests/boot/built_inputs.py` | The family's harness: testkit's runner harness plus a suite's built `inputs`, prepared before any case (on the x86 builder when the machine has one) and handed to the case as `NATEMU_INPUT_<NAME>`. |
| `tests/boot/run_kut.py` | The kvm-unit-tests cases: the references, the answers, the judge, the 16- and 32-bit code check. |
| `tests/boot/run_ovmf.py` | The OVMF cases: the references, the answers, the judge, the image's addresses. |
| `tests/boot/kvm_ref.py` | One KVM session on the reference machine (both suites). |
| `tools/reference/gdbrsp.py` | A GDB remote-protocol client for QEMU's gdbstub: stop at an address and read the CPU there, under TCG and KVM alike. |
| `tests/boot/first_version.py` | The first version's instruction list as a check over ndisasm (shared with the exit test). |
| `tests/boot/test_real_code.py` | Unit tests: the judges, the keys, the image parsing, the trace parsing, the GDB client against a fake stub, negative controls. |

**The inputs.** Each is a cache entry built once per machine in the builds' pinned amd64 Debian image (the toolchain
installed from snapshot.debian.org at 20260915T000000Z, every package checked against the recipe's `debs.sha256`),
holding the built files and `provenance.json`: `upstream` (each source's pinned sha256, the recipe folder's sha256, the
builder image), which keys the answers, and `built` and `builder` (what came out, on which machine), recorded and never
required. A rebuild elsewhere keeps the answers; a changed recipe or source needs new ones.

- **kvm-unit-tests** at a2891d9528eff31188bb16f08980637532735d1b (the plan's census commit), the GitLab archive of
  that commit: `x86/dummy.flat` and `x86/smptest.flat`, Multiboot v1 ELF32 images (about 250 KB each). About a minute
  on the x86 builder.
- **OVMF**, edk2-stable202608 (2970e5699ba6267f3384ffab20f96647578aebc8, August 21, 2026), the last release before the
  census's master 556dcfb466e9 (November 17, 2026); the GitHub archive of the commit plus its submodules openssl,
  brotli (used), mipisyst, mbedtls and libspdm (only for the include folders their packages declare). `OvmfPkgX64.dsc`,
  DEBUG, toolchain GCC, the DSC's defaults (4 MiB image, `PcdUse5LevelPageTable` FALSE) with `SMM_REQUIRE=FALSE` and
  `SECURE_BOOT_ENABLE=FALSE`. Outputs: `OVMF.fd` (4 MiB for FFC00000H-FFFFFFFFH), `OVMF_CODE.fd`, `OVMF_VARS.fd`,
  `SECFV.Fv.map`, `ResetVector.bin`, `report.txt` (the build's PCD and flash report). About a minute on the x86
  builder (the build itself 55 s).

## What runs now

Cases of `tests/boot/family.toml`, suites `kvm-unit-tests` and `ovmf`:

| Case | Reference | What it checks |
| --- | --- | --- |
| `kut-qemu` | QEMU 11.1.2, TCG, `max,-pcid,-smap` | smptest and dummy at 1, 2, 3 and 4 CPUs; each image's 16- and 32-bit code |
| `kut-kvm` | KVM on the x86 reference machine, `-cpu host,-pku`, Debian trixie's QEMU 10.0.13 | the same |
| `ovmf-qemu` | QEMU 11.1.2, TCG | reset vector to SEC's entry from the reset state and from `Main32` |
| `ovmf-kvm` | KVM on the x86 reference machine | the same |

A run (any tier) starts no QEMU and does not reach the x86 machine: each case looks up the locked answers for the
input's upstream and judges them (and `kut-*` checks the images' code as built). Seeding:
`TESTKIT_SEED_ANSWERS=1 NATEMU_X86_HOST=user@host tools/natemu-test run tests/boot -k "kut- or ovmf-"`, twice, then
`tools/natemu-test lock tests/boot --from-run RUN1 RUN2`. Each seeding run keeps the references' raw outputs (COM1, the
debug port, the GDB client's JSON, QEMU's trace) and the reference machine's description in the case's output folder.

**Results (seeded November 22, 2026, on the M2 Max and the x86 reference machine, AMD Ryzen 7 8845HS, kernel
7.0.0-43-generic, itself a KVM guest; the inputs built on the x86 builder; two seeding runs agreed,
`tests/boot/tests.lock`):** all pass, and QEMU's and KVM's answers are the same.

| Test | N | COM1 (sorted), both references; QEMU's exit status 1 (the test's status 0) |
| --- | --- | --- |
| smptest | 1-4 | `enabling apic` N times, `smp: waiting for N-1 APs`, `setup: CPU k online` for k = 1..N-1, `found N cpus`, `ipi called, cpu k` for k = 0..N-1, `PASS: IPI to each CPU`, `SUMMARY: 1 tests` |
| dummy | 1-4 | `enabling apic` N times, `smp: waiting for N-1 APs`, `setup: CPU k online` for k = 1..N-1, `Dummy Hello World!` |

The images' 16- and 32-bit code (both images alike): 61 instructions, all in the first version (below).

| Entry | Registers at SEC's first instruction, both references | Device accesses before | Debug port |
| --- | --- | --- | --- |
| reset | RIP = RSI = FFFD5E24H, RBP FFFCC000H, RDI 5042H (`BP`), RCX 1, RAX RBX RDX RSP R8-R15 0, RFLAGS 46H (AF, RF masked), CS 38H, SS DS ES FS GS 18H, FS/GS base 0, CR0 80000033H, CR3 800000H, CR4 660H, EFER 500H | none | `SecCoreStartupWithStack(0xFFFCC000, 0x820000)` |
| main32 | the same but RDI 0, CR0 E0000011H, CR4 20H | none | the same |

Shown to fail when they should: with the lock missing one answer, that case fails `ANSWER-MISSING`; with the OVMF
answers' CR3 changed (value and hash kept consistent), all four OVMF results fail; `test_real_code.py` refuses wrong
answers field by field, a trace that saw nothing, an image whose SEC entry differs from the answer's, port I/O in the
16- or 32-bit code, and a GDB client that reads the CPU without refreshing it.

## kvm-unit-tests: the cases and what natemu's side must provide

**How a test runs** (as upstream's `x86/run` runs it, with the environment natemu gives): Multiboot v1 through
QEMU's `-kernel` on machine `pc` with 128 MiB, the one module (`-initrd`) the text `NR_CPUS=<N>\n`, COM1 into a file,
`isa-debug-exit` at F4H, `-smp N`, `-display none` (with `-nographic`, SeaBIOS mirrors its banner onto COM1; natemu loads
the image itself, so the answer must be the test's output alone, and the judge refuses firmware text).

**What the tests read.** `cstart64.S` keeps EBX (the Multiboot information) and reads `mods_count`/`mods_addr` (the
module becomes the environment: `lib/x86/setup.c` `setup_multiboot`, `setup_libcflat`) and `mem_upper` (the
allocator's range), the command line for argv (`mb_cmdline`, argv[0] first: QEMU passes the image's path), and the CPU
count from the environment's `NR_CPUS` (`lib/x86/fwcfg.c`: the override; no fw_cfg read happens, so the board needs
none). `find_highmem`'s fw_cfg read of the RAM limit runs only under `setup_vm`, which neither test calls.

**The boot, by mode** (every 16- and 32-bit instruction listed by `kut-*` from the image as built: 61, all in the first
version's list):

- BSP, P32 from Multiboot (`start`, 8 instructions): `mov [mb_boot_info], ebx` (A3), `mov esp, imm`, GS_BASE WRMSR
  (`setup_percpu_area`), `call prepare_64`, then the far JMP ptr16:32 (`jmp 08H:start64`) in compatibility mode.
- `prepare_64` (27): `lgdt [edx]` (the 64-bit GDT), RDMSR/WRMSR of GS_BASE around `mov Sreg, ebx` for all five data
  segments, CR4 = 0 then PAE, CR3 from memory (`mov eax, [pt_root]`), EFER.LME (RDMSR, BTS, WRMSR), CR0.PE|PG; the
  near RET then runs in compatibility mode.
- APs, the 16-bit trampoline (5), copied to physical 0 and entered by SIPI vector 0 (CS 0000H, IP 0): `mov eax, cr0`,
  `or eax, 1`, `mov cr0, eax` (P16 from here), `o32 lgdt [0x18]` (DS base 0 from INIT), `jmp dword 08H:ap_start32`.
- APs, `ap_start32` (21): the segments as in `prepare_64`, `lock xadd [edx], esp` (each AP's stack from a shared top),
  GS_BASE, `call prepare_64`, then `push 08H; push edx; retf` (a 32-bit far RET from compatibility mode to 64-bit code).
- 64-bit code from then on (translated): IDT, `ltr`, xAPIC reset and enable, PIC masking, serial, and the start-up.

**What natemu must provide** (built on `boot-interp`: `crates/host` `boot::multiboot` and `--debug-exit`, cf40d9ee and
ac111723; CPU start-up, B2, ce8cea18):

- The Multiboot v1 loader: the ELF32 image at its physical addresses, the information structure with `mem_upper`, the
  memory map, the command line (the image's path first) and exactly one module, the environment text; EAX 2BADB002H,
  EBX the structure, flat 32-bit CS and data, paging off, IF = 0. RAM 128 MiB as the references (any size from about
  20 MiB works for these two tests; the output does not depend on it).
- The test port: `--debug-exit`, QEMU's isa-debug-exit at F4H: `exit()` writes its status with a 32-bit OUT (`outl`),
  and the run ends with QEMU's status (status << 1) | 1, so a pass is 1. Writes to 8900H (Bochs power-off) follow only
  if F4H did not end the run.
- COM1 (16550A at 3F8H): LCR with DLAB, the divisor, IER, FCR, MCR writes, LSR polled for THRE before each byte.
- PIC ports 21H and A1H written (masked), from 64-bit code.
- The local APIC: IA32_APIC_BASE cleared of EN and EXTD and then set to EN (`reset_apic`: xAPIC), SVR at FEE000F0H
  through MMIO (`enable_apic`, which prints `enabling apic`), and the start-up IPIs through the **xAPIC** MMIO ICR:
  ICR high FEE00310H, ICR low FEE00300H after polling its busy bit, INIT (assert) then one start-up IPI, vector 0, both
  all-excluding-self, back to back with no delay (the plan's latched SIPI). Only afterwards `enable_x2apic` sets EXTD;
  the IPIs of `smp_init` and `on_cpu` are then fixed IPIs, vector 20H, physical destination, through x2APIC MSR 830H,
  each taken by an AP in its `sti; hlt` loop (64-bit IDT delivery) and answered with an EOI.
- An AP from SIPI: the INIT state (plan section 9), CS 0000H base 0 IP 0, DS base 0; the interpreter through R, P16,
  P32 and C32 as above; the far RET into 64-bit code hands it to the translator.
- CPUID as the profile gives it (`pmu_init` reads leaf 0AH, 0 in the profile; PDCM is clear, so no
  IA32_PERF_CAPABILITIES read).

**The natemu cases** (one per test and N, 1 to 4 vCPUs, no mailbox): boot the image with the module and
`--debug-exit`; build the answer with `run_kut.answer(status, COM1)` from natemu's debug-exit status and serial output;
pass when `run_kut.judge(test, n, answer, want_exit=<natemu's status for a write of 0, 1 like QEMU's>)` is empty and the
answer's `lines` equal the locked reference answer's (`run_kut.key(test, n, 'kvm', provenance)`; QEMU's and KVM's
agree). The lines are sorted because the CPUs print concurrently.

## OVMF: from the reset vector to SEC

**The observable.** The CPU at SEC's first instruction: QEMU starts paused at reset with a gdbstub,
`tools/reference/gdbrsp.py` sets a hardware breakpoint at SEC's entry, continues, and reads the registers there; it then
lets SEC run until the debug port (402H, isa-debugcon) holds SEC's first line, and QEMU's `memory_region_ops` trace
gives every device access the CPU made before the stop. The answer: the stop, the registers (EFLAGS without AF, left
undefined by the AND just before the jump, and RF), the device accesses before SEC's entry, and the debug port's lines up
to SEC's first. The judge checks them against what the reset vector's source gives (`run_ovmf.expected_registers`).

Two findings about the rig, both handled in `gdbrsp.py`: under KVM, QEMU's `p` packet reads QEMU's copy of the CPU,
which only `g` refreshes (without it every read gave the reset state, so it sends a `g` at every stop); and QEMU 11's
trace times are ISO dates where QEMU 10's are `PID@seconds`. A trace that shows no access at all after the stop (SEC
always reads and writes the debug port) proves nothing and fails the judge.

**Entries.**

- `reset`: the power-on state, as QEMU's reset gives it.
- `main32`: the same image entered at FFFFFFF0H with CR0.PE already set, the state OVMF's `ResetVectorVtf0` tests with
  `mov eax, cr0; test al, 1` (TDX starts there): the rig writes a GDT at physical 0 (null, 08H flat 32-bit code, 10H
  flat data; the reset GDTR covers it), then CR0 = 60000011H (the power-on CR0 with PE), CS = 08H, SS, DS, ES, FS,
  GS = 10H, EIP = FFFFFFF0H; QEMU loads the hidden parts from the GDT. QEMU and KVM both run it: `Main32` works off
  TDX (the census's **Inferred**, now shown).

**The image's addresses** (`run_ovmf.py` reads them from the build: SEC's entry from `SECFV.Fv.map`, the boot firmware
volume by the reset vector's own search, the two jump targets from the reset vector's 16 bytes; fixed addresses from
`report.txt`):

| What | Address |
| --- | --- |
| Image | FFC00000H-FFFFFFFFH (4 MiB; VARS 528 KiB at the bottom, then FVMAIN_COMPACT, SECFV from FFFCC000H) |
| Reset vector | FFFFFFF0H: `mov eax, cr0; test al, 1; jz .Real; jmp Main32 (32-bit); .Real: jmp EarlyBspInitReal16` |
| `EarlyBspInitReal16` | FFFFFEF0H (CS F000H, IP FEF0H) |
| `Main32` | FFFFFF08H |
| Boot firmware volume (SECFV) | FFFCC000H |
| SEC's entry (`SecMain` `_ModuleEntryPoint`) | FFFD5E24H (image base FFFCC094H) |
| Page tables (`PcdOvmfSecPageTablesBase`) | 800000H-805FFFH: PML4, PDPT, four PDs of 2 MiB pages, identity up to 4 GiB |
| Work area (`PcdOvmfWorkAreaBase`) | 80B000H (its guest-type byte); SEV-ES work area 80B004H (25 bytes cleared) |
| SEC's temporary RAM | 814000H-81FFFFH (ESP set to 820000H by `SevCpuidInit`, never pushed to before SEC) |

**The path and what it touches.** 16-bit (R, then P16), only on `reset`: the reset vector's jump, `mov di, 'BP'`,
`mov esp, eax`, `cli`, DS = F000H, `o32 lgdt [cs:bx]` through the reset CS base, CR0 = 23H (PE, MP, NE; CD and NW
cleared), far JMP ptr16:32 to 10H; then CR4 = 640H and the flat data segments 18H, and the work area's guest-type byte.
`Main32` instead reloads the same GDT with `lgdt [eax]` and far-jumps to 10H (P32 from the start). Then, on both, 32-bit
with paging off: `SevCpuidInit` (ESP, `lidt [cs:eax]`), `IsTdx` (CPUID 0, 1, and 21H only when the maximum leaf
reaches it), the BFV search (FV headers from FFFFF000H down in 4 KiB steps), the SEC search (FFS file headers, SEC's PE
header), `CheckSevFeatures` (the work area cleared, CPUID 80000000H, and 8000001FH only when the maximum extended leaf
reaches it), the page-table build (6,144 dword stores to clear, then 2,048 2 MiB entries: C7 stores, `loop`), CR3,
CR4.PAE, EFER.LME (RDMSR/WRMSR), `bt [mem], ecx` on the SEV-ES status, CR0.PG; in compatibility mode `lidt [cs:eax]`
(`SevCpuidExit`: base 0, limit 0), `xor eax, eax; mov esp, eax` and the far JMP ptr16:32 to 38H; in 64-bit mode a
compare of its own code, the upper halves of RSI, RBP and RSP cleared, `jmp rsi`. No stack, no near CALL or RET (the
reset vector's `OneTimeCall` is a jump pair), IF = 0 throughout.

**Device accesses before SEC: none**, on both references and both entries: no I/O port (assigned or not), no MMIO page.
The reset vector's debug output is compiled out (`DebugDisabled.nasm.inc`: `DEBUG_PORT80` and `DEBUG_SERIAL` are not
defined, and `QemuDebugCon.nasm.inc` sits behind `%elif 0`); no CMOS or fw_cfg read happens before SEC; SEV and TDX
paths (VMGEXIT through MSRs, `rdrand`) are not taken off those platforms. SEC's own first accesses, in 64-bit code, are
PCI configuration (CF8H/CFCH: the host bridge's device ID), the debug port 402H (read back E9H, then its text), CMOS
(70H/71H), the PICs (21H, A1H masked), the local APIC timer and fw_cfg (510H, DMA): the firmware boot mode's devices
for M3, not the interpreter's.

**What natemu's firmware boot mode must provide for this image:**

- **Load:** `OVMF.fd` at guest-physical [FFC00000H, 4 GiB), read and fetched, writes ignored (nothing before SEC
  writes it; DXE's variable driver writes later, M3's concern). No copy below 1 MiB is needed.
- **Reset state of the BSP:** the SDM's power-on state (plan section 6): CS F000H base FFFF0000H limit FFFFH, EIP FFF0H,
  CR0 60000010H, EFLAGS 2, EDX the signature, the other segments base 0 limit FFFFH, GDTR and IDTR base 0 limit FFFFH,
  the MTRRs at their power-on values (SEC enables them in 64-bit code: `IA32_MTRR_DEF_TYPE` WB with E). Until then RAM
  is UC under natemu's exact rule (plan section 4), so the page-table loops (about 27,000 instructions) run uncached.
- **The `Main32` start (an option of the firmware boot mode or the program driver):** EIP FFFFFFF0H with CR0
  60000011H, CS 08H with base 0, limit FFFFFFFFH, attributes C09BH (G, D, P, S, code read/execute accessed), SS DS ES FS
  GS 10H with base 0, limit FFFFFFFFH, attributes C093H; everything else as at power-on. The GDT the QEMU rig writes at
  0 is the rig's way to give QEMU those hidden parts; natemu sets them directly and OVMF never reads 0-17H.
- **RAM:** from 0, at least 80C000H for the reset vector (the page tables and the work area); SEC then needs its
  temporary RAM, the decompression scratch up to `PcdOvmfDecompressionScratchEnd` (1A10000H) and the DXE volume at
  900000H-177FFFFH: 32 MiB or more (the references use 256 MiB). The legacy hole is not touched before SEC.
- **CPUID:** leaf 0 (vendor; a maximum basic leaf below 21H skips the TDX probe), leaf 1 (ECX bit 31), 80000000H (a
  maximum extended leaf below 8000001FH skips the SEV probe; natemu's profile has 15H and 80000008H). MSRs: EFER read
  and written in P32 (LME), nothing else before SEC.
- **Instructions:** the plan's section 1 list covers the path (the census, replicated); the new mode facts it relies
  on: CR4 640H (MCE, OSFXSR, OSXMMEXCPT) under PG = 0, LIDT in P32 and in C32, MOV r/m←imm (C6/C7), `bt [mem], ecx`,
  `loop`, `movzx`, a far JMP ptr16:32 from P16, from P32 (`Main32`) and from C32 to 38H.
- **No port I/O and no device page before SEC:** the interpreter's first version stops at either; this path has none.

**The natemu case** (per entry): run the image in the firmware boot mode (`main32`: with the start state above) until
the CPU first reaches SEC's entry in 64-bit mode, and compare its registers with the locked answer
(`run_ovmf.key(entry, 'kvm', provenance)`; QEMU's and KVM's agree), judged by `run_ovmf.judge` (the same expected
registers). What natemu needs for that: a stop at a linear address with the registers reported (a debug option of the
run, as the interp cases' stops), or the debug port 402H (reads E9H, writes printed) so that SEC's first line shows, or
both. The case's device list comes from natemu's own record of port and device accesses before the stop, which must be
empty.
