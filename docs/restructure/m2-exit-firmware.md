# M2 exit test: the firmware and its AP part

Status (November 22, 2026, `boot-interp` commits a120f189 to aaa65e74): built by the framework from the repository and
proven on the pinned QEMU 11.1.2 (TCG) and on real hardware through KVM, at 1, 2 and 4 CPUs. natemu runs the program
variant whole at 1, 2 and 4 CPUs, each AP's stub in the boot-time interpreter (case `exit-ap-natemu`: the CPU start-up
slice, B2 of the boot-time interpreter plan); the firmware image runs whole in natemu's firmware boot mode at 1, 2 and
4 CPUs ([firmware-mode.md](firmware-mode.md), case `exit-fw-natemu`).
This page is the contract those slices build to.

## The pieces

| File | What it is |
| --- | --- |
| `tests/boot/exit_fw.asm` | The firmware: a 64 KiB image for the top of the 4 GiB space (FFFF0000H-FFFFFFFFH). |
| `tests/boot/exit_prog.asm` | The AP part as a 64-bit program, for the program driver before the firmware boot mode exists. |
| `tests/boot/exit_ap.inc` | What both share: the AP stub's page image (`AP_STUB`) and the BSP's sender (`START_APS`). |
| `tests/boot/run_exit_fw.py` | The runner: assembles both with nasm, runs them on a reference, judges, seeds or checks the lock. |
| `tests/boot/test_exit_fw.py` | Unit tests: layout, the first-version instruction check (ndisasm), the judges, negative controls. |

nasm is a declared prerequisite (README: `brew install nasm container`; `ndisasm` comes with it). The runners assemble
at run time from the repository, as the boot smoke's 32-bit program and the smp stub are; the answers are keyed by the
image's sha256.

## The firmware

The BSP, from the reset vector (CS F000H, base FFFF0000H, IP FFF0H; `jmp near` to the 16-bit entry):

1. **16-bit (R, then P16):** `cli`, `cld`; WRMSR IA32_MTRR_DEF_TYPE = 806H (E = 1, FE = 0, default type WB: until then
   all memory is UC, plan section 4); `o32 lgdt [cs:...]` through the reset CS base; CR0.PE set with CD and NW cleared;
   `jmp dword 10H:...` (far JMP ptr16:32 under the stale real-mode CS). 13 instructions, the reset vector's jump
   included.
2. **32-bit, paging off (P32):** flat segments; identity page tables for all of 4 GiB at 10000H (PML4, PDPT, four PDs,
   2 MiB pages, present and writable, no NX); CR4.PAE, CR3, EFER.LME (RDMSR/WRMSR), CR0.PG; one far JMP in compatibility
   mode (C32) to the 64-bit CS. 36 instructions in the image, about 16,400 executed (two loops of 2,048).
3. **64-bit (translated code), from the ROM:** stack at 80000H; COM1 set to 8N1 without interrupts; `EXITFW long mode`;
   `START_APS`; the report; power-off.

`START_APS` (also the program's body): copies the AP stub (464 bytes) to 8000H, writes the BSP's CR3 (must be below
4 GiB) and its EFER without LMA into the stub's fields, reads the CPU count, enables the x2APIC (IA32_APIC_BASE EN|EXTD,
SVR 1FFH), writes the ICR (MSR 830H) with INIT (assert), waits 10,000,000 TSC cycles, SIPI vector 08H, waits 200,000,
SIPI again, waits 200,000, all with the all-excluding-self shorthand and each after `mfence; lfence` (a WRMSR to an
x2APIC register is not serializing, SDM Vol.3A 11.12.3: the stub and its fields are visible before the IPI). It then waits until the counter reaches the CPU
count - 1 (timeout 2^34 TSC cycles), waits 20,000,000 cycles more (an AP started twice counts twice), and tallies.

Each AP, from the SIPI (CS 0800H, base 8000H, IP 0): `cli`; the same MTRR write; `mov ax, cs; mov ds, ax`; `o32 lgdt`
of the stub's own GDT; PE (CD, NW cleared); far JMP ptr16:32 (13 instructions in R and P16); flat segments, CR4.PAE,
CR3 and EFER from the stub's fields, PG, far JMP to the 64-bit CS (17 instructions in P32 and C32); in 64-bit mode
`lock inc` of its arrival byte (indexed by its initial APIC ID) and of the shared counter, then `cli; hlt` in a loop.
Both updates are RIP-relative: the same bytes run as 32-bit code address low memory instead, so an AP that is not
really in 64-bit mode after its last far jump (an interpreter that carries on in compatibility mode, say) never
arrives and the BSP reports a timeout (`test_exit_fw.py` checks the encoding).
The stub page holds 16-, 32- and 64-bit code, the GDT, the BSP-written fields, the counter and 256 arrival bytes: code
and data on one page, on purpose (plan section 5).

Every 16- and 32-bit instruction is in the first version's list (plan section 1); `test_exit_fw.py` disassembles each
region and refuses anything else (string instructions, port I/O, far CALL, 16-bit far RET, CR2, debug registers, PUSHA,
segment push and pop, ...), with a negative control for the check itself. No port I/O outside 64-bit code. Every
descriptor's accessed bit is preset, so no CPU writes a descriptor (in the ROM or on the stub page).

**The report,** two lines on COM1 (CR LF):

    EXITFW long mode
    EXITFW cpus=<CPUs expected> aps=<APs arrived> ids=<APIC IDs that arrived once, comma-separated, or -> status=<n>

status 0: every AP arrived exactly once; bit 0: timeout; bit 1: an AP arrived more than once, the BSP's own ID was
marked, an ID of 64 or more arrived, or the counter exceeds the APs expected; bit 2: CR3 above 4 GiB (nothing sent).
At N CPUs the pass is `cpus=N aps=N-1 ids=1,...,N-1 status=0`.

**Power-off:** a 16-bit write of 2000H (SLP_EN, SLP_TYP 0) to port B004H, natemu's PM1a control register (`_S5`'s
SLP_TYP is 0: `crates/host/src/pm.rs`); then a byte write of 0 to port F4H, QEMU's `isa-debug-exit` in the reference
runs (exit status 1; unassigned on natemu, never reached there); then `cli; hlt` in a loop.

## The program variant

`exit_prog.asm` assembles one body at 110000H: the AP stub's image (data, copied by the program), then the program at
111000H, its start RIP. It runs `START_APS` and ends at the body's end with RAX = CPUs expected, RBX = APs arrived,
RCX = status, RDX = bitmap of the APIC IDs that arrived once, every other GPR zero except RSP (no stack is used), and
the flags of `cmp eax, eax`. On the references it runs through `tools/bench/smp`'s stub (Multiboot through SeaBIOS,
CPL 0, 64-bit, the stub's identity map of the first 4 MiB) with one CPU in the stub's configuration, so the stub starts
no CPU, and QEMU's `-smp N`, so the program starts the others.

**What the program driver must give it (B2).** Today every vCPU of a description starts running its own section; the
ACPI mailbox is opt-in per vCPU (`mailbox GPA`, X4), and the driver builds no MADT, so leaving the mailbox out needs
nothing. B2 adds the missing piece: the `cpu` section item `wait-for-sipi`, which starts the vCPU in the wait-for-SIPI
state (the power-on state, no other state items). The description is then:

    identity
    body 0x110000 <exit_prog.bin>
    region 0x8000 0x1000           (the stub page: writable and, with nxe 0, executable)
    cpu
    rip 0x111000
    cpl 0
    nxe 0                          (the APs copy the BSP's EFER; the tables carry no NX bit)
    gpr 0 0 0 0 0x3e1800 0 ...
    cpu                            (one per AP)
    wait-for-sipi

The driver's tables (placed from `--place`, 1000H by default) are below 4 GiB, as the stub's 32-bit `mov cr3` needs.
The pass is vCPU 0 reaching its end with the registers above; each AP ends halted in its park loop. `exit-ap-natemu`
(`run_exit_fw.py --backend natemu`) passes at 1, 2 and 4 CPUs: each AP climbs from its SIPI in the boot-time
interpreter (16 -> 32 -> compatibility mode) to 64-bit code and arrives once, counted from there; at one CPU the IPIs
select no CPU.

## What natemu's firmware boot mode must provide

- **Load:** the image at guest-physical [4 GiB - size, 4 GiB) (here FFFF0000H, 64 KiB), read and fetched, writes
  ignored. No copy below 1 MiB is needed: nothing runs from or reads F0000H.
- **Reset state of the BSP (vCPU 0):** the SDM's power-on state (plan section 6): CS F000H, base FFFF0000H, limit FFFFH,
  EIP FFF0H, CR0 60000010H, EFLAGS 2 (IF = 0), the other segments base 0 limit FFFFH; IA32_APIC_BASE FEE00900H (xAPIC
  enabled, BSP), the x2APIC advertised and enterable from xAPIC. The MTRRs at their power-on values
  (IA32_MTRR_DEF_TYPE 0, no valid variable range), not the virtual firmware's `MTRR_INITIAL` of today's boots: the image
  sets E and WB itself, and a valid variable range left over would make the ROM's 64-bit code UC.
- **The other vCPUs:** in wait-for-SIPI from the start (the INIT state), APIC_BASE FEE00800H, the same MTRRs; no ACPI
  mailbox (the image reads no ACPI table; no MADT is needed).
- **RAM:** from 0, at least 1 MiB: the image uses 8000H-8FFFH (the AP stub page; SIPI vector 08H), 10000H-15FFFH
  (page tables) and 70000H-7FFFFH (the BSP's stack). The legacy hole is not touched.
- **Paging:** 2 MiB identity pages over all of 4 GiB, so the BSP's 64-bit code runs from the ROM at FFFFxxxxH under
  the leaf FFE00000H-FFFFFFFFH, which holds addresses with nothing behind them besides the image; other leaves cover
  the local APIC's page (never accessed: the x2APIC is used) and the rest of the space above RAM.
- **The CPU count:** CPUID.1:EBX[23:16] = N with EDX.HTT = (N > 1), as `natemu_abi::cpu::cpuid` presents it today
  (QEMU and KVM give the same for powers of two). APIC IDs are the vCPU numbers; the report's bitmap holds IDs below 64.
- **x2APIC:** MSRs 80FH (SVR) and 830H (ICR) with INIT (level assert) and start-up, shorthand all excluding self, from
  64-bit code.
- **TSC:** RDTSC in 64-bit code; the delays and the timeout are TSC cycles (at natemu's 1 GHz: INIT 10 ms, each SIPI
  0.2 ms, settle 20 ms, timeout 17 s).
- **Serial:** COM1 (16550A at 3F8H): THR, IER, LCR, and LSR's THRE (bit 5) polled before each byte; 64-bit code only.
- **Power-off:** PM1a control at B004H, SLP_EN with SLP_TYP 0, as today's PM block (`crates/host/src/pm.rs`) does; the
  run ends with the typed stop `poweroff`. APs halted in their park loop (IF = 0) at that point.

## What runs now

Cases of the `exit-fw` suite in `tests/boot/family.toml`, each at 1, 2 and 4 CPUs (one result per N):

| Case | Reference | Image | Pass |
| --- | --- | --- | --- |
| `exit-fw-qemu` | QEMU 11.1.2 (the framework's pinned build), TCG, `max,-pcid,-smap`, `-bios` | firmware | the two report lines, exit 1 |
| `exit-fw-kvm` | KVM on the x86 reference machine, `-cpu host`, Debian trixie's QEMU (snapshot 20260915T000000Z) | firmware | the same |
| `exit-ap-qemu` | QEMU 11.1.2, TCG, through the smp stub (`-kernel`) | program | CPU 0's registers, exit 1 |
| `exit-ap-kvm` | KVM as above, through the smp stub | program | the same |

A run (any tier) does not start QEMU or reach the x86 machine: each case assembles the images, looks up the locked
answer for the image as built and checks it is the pass. An image changed since seeding has no answer
(`ANSWER-MISSING`): reseed with `TESTKIT_SEED_ANSWERS=1 NATEMU_X86_HOST=user@host tools/natemu-test run tests/boot -k
"exit-fw or exit-ap"`, twice, then `tools/natemu-test lock tests/boot --from-run RUN1 RUN2`.

**Results (seeded November 22, 2026, on the M2 Max and the x86 reference machine, AMD Ryzen 7 8845HS, kernel
7.0.0-43-generic, Debian's QEMU 10.0.13 for KVM; two seeding runs agreed, `tests/boot/tests.lock`):** all twelve pass.

| N | Firmware's COM1 report (TCG and KVM alike) | Program's CPU 0 (TCG and KVM alike) |
| --- | --- | --- |
| 1 | `EXITFW cpus=1 aps=0 ids=- status=0` | RAX 1, RBX 0, RCX 0, RDX 0 |
| 2 | `EXITFW cpus=2 aps=1 ids=1 status=0` | RAX 2, RBX 1, RCX 0, RDX 2H |
| 4 | `EXITFW cpus=4 aps=3 ids=1,2,3 status=0` | RAX 4, RBX 3, RCX 0, RDX 0EH |

Shown to fail when they should: a run with the image changed after seeding fails every case with `ANSWER-MISSING`;
`test_exit_fw.py` refuses wrong reports and registers, checks its instruction check against forms outside the list
(and that it accepts the list's own forms), and checks that the AP's counting instructions, decoded as 32-bit code,
address neither the counter nor the arrival bytes.
Experiment (not a case; the x86 reference machine, KVM, the dev container's Debian QEMU 10.0.13, by hand): a firmware
whose AP never adds to the counter reports `cpus=4 aps=0 ids=1,2,3 status=1` (timeout) at 4 CPUs; one whose AP stops
at a real-mode `hlt` (IF = 0) before its far jump reports `aps=2 ids=1,3 status=1`: KVM woke two of the three halted
APs at the second SIPI, which the SDM does not do and which the plan's rule for natemu (section 6: a SIPI does not wake
HLT) does not either. The exit test's APs park in 64-bit code in a `cli; hlt` loop, so either behaviour passes it.

## Notes for the slices that run it under natemu

- Today `lapic.rs` stops `unsupported_system` at any INIT or start-up IPI, before looking at the targets: at 1 vCPU,
  the all-excluding-self INIT has no target and must become a no-op, not a stop.
- The CPU start-up slice's case: the program at 1, 2 and 4 vCPUs, judged by `run_exit_fw.judge('program', N, answer,
  want_exit=...)` on the driver's final registers, with the answer's `exit` set from vCPU 0's stop (its end reached)
  in place of QEMU's exit status. The firmware boot mode's case: the image at 1, 2, 4 and more vCPUs, judged by
  `judge('firmware', N, answer, want_exit='poweroff')` on natemu's serial output and its `poweroff` stop; the plan's
  bounds on decode exits and shootdowns (section 8) are asserted there.
- The stub page holds the APs' 64-bit code and the counter: once one AP has translated its 64-bit code there, the
  other APs' counter stores go through `code_write` while later APs may still be interpreting their 16-bit code on
  the same page (plan section 5); that is what the case exercises.
