# S3g plan: 32-bit flat protected mode and mode transitions

Status: design survey, October 28, 2026 (read-only; no code changed). **Owner decision (October 28): all 32-bit support is deferred to the boot phase** (slice-3 contract §5 item 3); this plan is the boot phase's starting point if its kernel needs 32-bit or paging-off code. Builds on the [slice-3 contract](slice3-contract.md)
§3 ("S3g stays in this phase"), §4 S3g and §5 decision 1, and the [contract inventory](contracts.md) items 22, 27–33.
Scratch evidence: `/private/tmp/claude-501/-Users-liaozping-orca-natemu/025ed521-9978-4fb2-aaf7-b90da95d37f3/scratchpad/s3g0/`
(`src/`: the upstream source files read; `tramp32.lst`, `tramp64.lst`: encodings of the transcribed trampoline).
**Later change (S3i, October 28):** the families this plan would have let S3i delete are deleted under decision 3; the
models it cross-checks against are now `tools/models/pae_model.py`, `modes_model.py` and `coherence_model.py`; the deleted
code (32-bit families, `paging_off32`, `controls`, `modes`, `pae`, `mixed32`) is in git history at `ed11ab1`. The legacy
two-level walker `crates/abi/src/paging32.rs` (PSE off) had no general-path caller: deleted at S3i1; recover it from commit
a6b299b (`crates/abi/src/paging32.rs`) if the boot phase needs legacy paging.

## 1. Which kernel code runs in 32-bit mode

**No kernel binary exists locally.** The project has no single pinned image yet (I5 "selects an exact stock kernel").
The records name two working targets: Debian `6.12.107+deb13-amd64` (installer `linux`, 12,142,528 bytes, SHA256
`2b2358b3…0e08`, INV-03-03 claim 13; the reading copies under `/private/tmp` are gone) and Ubuntu GA `6.8.0-139`
(INV-01-34 claims 08–09). Following the brief, the survey reads the upstream source of the pinned version
(`gregkh/linux` tag v6.12.107; Debian's patches do not touch these files, INV-01-34 claim 09) and v6.8 for Ubuntu.
Encodings below come from assembling a transcription of the 32/64-bit trampoline with the host clang (`tramp32.lst`);
link-time addresses are unknown without the binary, so the "where" column is the source line.

**Planned boot method** (INV-03-03 loader recipe): the 64-bit boot protocol (enter `K+0x200` in long mode, LA57=0,
flat CS 0x10/DS 0x18) for the BSP; APs through the ACPI MADT version-0 mailbox (`trampoline_start64`), never INIT/SIPI.

| Path | 32-bit code reached? |
| --- | --- |
| BSP: decompressor `startup_64` → `configure_5level_paging` (`boot/compressed/pgtable_64.c:106–144`) | **No**, while CPUID reports no LA57: `l5_required` is false and equals CR4.LA57=0, so `trampoline_32bit_src` (the LA57 toggle) is skipped. |
| BSP: decompressor `startup_32` (`boot/compressed/head_64.S:83–281`) | **No**: only the 32-bit boot protocol enters there. |
| BSP: kernel `head_64.S` | **No**: `.code64` throughout. |
| AP, Debian 6.12: `trampoline_start64` (`realmode/rm/trampoline_64.S`) | **No**: it compares CR4.LA57 with `tr_cr4` and on a match stays in 64-bit mode (lidt, lgdt, LOCK BTS lock, MOV SS/DS/ES/FS/GS, MOV CR3, `lretq`). The 32-bit route runs only on an LA57 mismatch, impossible with LA57=0. |
| AP, Ubuntu 6.8: `trampoline_start64` (v6.8) | **Yes, always**: `ljmpl *tr_compat(%rip)` → compatibility mode → paging off → `startup_32` → PAE/LME/PG → `ljmpl` to 64-bit. |

So with the Debian kernel and the planned CPU profile, **no 32-bit or paging-off instruction executes**. The 32-bit
consumer among the named targets is Ubuntu 6.8's AP start (and any kernel/loader choice that uses the 32-bit boot
protocol). Section 5 brings this to the owner.

## 2. Transaction list

Modes: **64** = 64-bit; **C** = compatibility (LMA=1, CS.L=0, CS.D=1, 4-level paging, 32-bit linear); **P0** = 32-bit
protected, paging off; **PP** = 32-bit protected with PAE paging (LMA=0); **L** = legacy two-level. All code here runs at
CPL0 with flat descriptors (base 0, limit 4 GiB, G=1): `__KERNEL32_CS` 0x08 = `00cf9b000000ffff`, `__KERNEL_CS` 0x10 =
`00af9b…`, `__KERNEL_DS` 0x18 = `00cf93…` (A bits already set, so no descriptor write).

### A. Realmode trampoline, 32-bit route (Ubuntu 6.8 APs always; Debian 6.12 only on an LA57 mismatch)

| # | Instruction (bytes) | Mode | Operands | Resident must check / do (SDM) |
| --- | --- | --- | --- | --- |
| A1 | `ljmpl *tr_compat(%rip)` (`ff 2d d32`) | 64 | m16:32 (6 bytes, RIP-relative) | JMP far m16:32 (no REX.W; Intel also m16:64 with REX.W). Read 6 bytes via the data walk (#PF); selector null → #GP(0); TI=1 → refuse; index past GDTR.limit → #GP(sel); read descriptor through paging (#PF, implicit supervisor); S=0 (gate/TSS) → refuse; not code → #GP(sel); nonconforming RPL>CPL or DPL≠CPL → #GP(sel); L=D=1 → #GP(sel); not present → #NP(sel); set the descriptor A bit if clear. Commit CS, EIP (32-bit target); mode becomes C. |
| A2 | `lock btsl $0, pa_tr_lock` (`f0 0f ba 2d m32 00`), `pause`, `jnc`, `jmp` | C | m32 absolute | **LOCK BTS is not admitted** (S3e: boot phase). PAUSE, BT, branches are ordinary code. |
| A3 | `movl $rm_stack_end,%esp`; `movw $0x18,%dx` (`66 ba`) | C | — | Ordinary code (32-bit profile). |
| A4 | `movl %eax,%cr0` (`0f 22 c0`), EAX = `CR0_STATE & ~PG` = 0x50033 | C | r32 | MOV CR0 clearing PG in C: allowed (in 64 → #GP(0)); LMA := 0; PE stays 1 (PE=0 → real mode: refuse). CR0 MP/NE/AM set: accept as state. Flush; mode becomes P0. |
| A5 | `ljmpl $0x08,$pa_startup_32` (`ea p32 0800`) | P0 | ptr16:32 | JMP far ptr16:32 (64 → #UD). Same descriptor checks as A1 without the L/D rule (LMA=0; D=1 else 16-bit → refuse); offset ≤ limit. |
| A6 | `movl %edx,%ss/%ds/%es/%fs/%gs` (`8e d2/da/c2/e2/ea`) | P0 | r32, selector 0x18 | MOV Sreg: CS destination or LOCK → #UD; SS null → #GP(0); TI=1 → refuse; past limit → #GP(sel); SS: RPL≠CPL, DPL≠CPL or not writable data → #GP(sel), not present → #SS(sel); DS/ES/FS/GS: null loads null; not data/readable code or (nonconforming) RPL/CPL > DPL → #GP(sel); not present → #NP(sel). Set A bit if clear. |
| A7 | `btl $0,pa_tr_flags` (`0f ba 25 m32 00`), `jnc` | P0 | m32 | Ordinary code; with SME absent the SYSCFG `rdmsr`/`bts`/`wrmsr` (MSR 0xc0010010) is skipped. |
| A8 | `movl pa_tr_cr4,%eax` (`a1`); `movl %eax,%cr4` (`0f 22 e0`) | P0 | r32 = kernel `mmu_cr4_features` | MOV CR4 with PG=0: PAE set (no PDPTE load: paging off). **The value carries Linux's feature bits (PGE, PSE, OSFXSR, OSXMMEXCPT, …), refused today** (S3c boot-phase item). |
| A9 | `movl $pa_trampoline_pgd,%eax`; `movl %eax,%cr3` (`0f 22 d8`) | P0 | r32 | MOV CR3, paging off: store only (no PDPTE load, no flush needed beyond policy). |
| A10 | `movl $0xc0000080,%ecx`; `rdmsr`; `cmp m32`; `jne`; `cmp`; `je`; `movl m32`; `wrmsr` | P0 | EFER | RDMSR/WRMSR EFER: EDX reserved → #GP(0); LME change allowed (PG=0); LMA write ignored; NXE/SCE state. |
| A11 | `movl $CR0_STATE,%eax` (0x80050033); `movl %eax,%cr0` | P0 | r32 | MOV CR0 setting PG with LME=1: CR4.PAE=0 → #GP(0); CS.L=1 → #GP(0); TR a 16-bit TSS → #GP(0) (Intel); PE=0 → #GP(0); NW without CD → #GP(0). LMA := 1, **no PDPTE load** (IA-32e). Flush; mode C. |
| A12 | `ljmpl $0x10,$pa_startup_64` (`ea p32 1000`) | C | ptr16:32 | JMP far ptr16:32 to an L=1 segment: checks of A5 plus L=D=1 → #GP(sel); mode becomes 64. |
| A13 | `jmpq *tr_start(%rip)` | 64 | — | Ordinary indirect branch. |

### B. Decompressor LA57 toggle `trampoline_32bit_src` (only when CPUID LA57 ≠ CR4.LA57: unreachable with LA57=0)

`pushq $0x08; pushq %rax; lretq` (64 → C: **far RET**, `48 cb`); `movl %cr0,%eax; btrl; movl %eax,%cr0` (PG off in C);
`movl %edi,%cr3`; EFER `rdmsr/btsl/jc/wrmsr`; `movl %cr4,%eax; btcl $12; movl %eax,%cr4` (**LA57 toggle with PG=0**);
`movl %cr0; btsl; movl %eax,%cr0` (PG on: IA-32e); `ljmpl $0x10,…` (C → 64). New relative to A: MOV from CR0/CR4 in 32-bit
(r32 ← low 32 bits), far RET (pop RIP and CS, 8+8 bytes with REX.W, 4+4 without; return CS checks as A1 plus RPL<CPL →
#GP(sel), RPL>CPL is an outer-privilege return: refuse), LA57 (not in the profile: reserved, #GP).

### C. Decompressor `startup_32` (32-bit boot protocol only: not the planned loader)

`cld; cli` (CLD: unsupported form; CLI: interrupt flag, boot phase); `call 1f; popl` (ordinary); `lgdt (%eax)` (32-bit
LGDT: CPL>0 → #GP(0), register operand/LOCK → #UD, 6-byte read via data walk, 66 prefix → 24-bit base); MOV DS/ES/FS/GS/SS
with `__BOOT_DS` (descriptors `0x9a`/`0x92`: **A bit clear → the CPU writes it into the GDT**); `pushl; pushl; lretl` (far
RET in P0, 4+4 bytes); `verify_cpu`: `pushfl; pushl $0; popfl` (**PUSHF/POPF**), `cpuid` ×5–7 (**CPUID**, vendor-dependent
`rdmsr/wrmsr` IA32_MISC_ENABLE on Intel family ≥ 6/model ≥ 0xd); `startup32_load_idt` (LIDT) and `get_sev_encryption_bit`
(CPUID 0x8000001f); `movl %cr4; orl PAE; movl %cr4`; **`rep stosl`** to clear tables, ordinary stores to build them;
`movl %eax,%cr3`; EFER `rdmsr; btsl LME; wrmsr`; `lldt` (null); **`ltr %ax`** (TSS type 9 → busy bit written into the
GDT); `pushl $__KERNEL_CS; pushl; movl $CR0_STATE,%cr0` (IA-32e activation); `lret` (C → 64, far RET).

### D. 64-bit transactions on the reached path (boot phase; S3g's code serves them if written mode-generically)

`lidt`/`lgdt` m16:64, `mov %cr4,%rax`, LOCK BTS (RIP-relative lock), MOV SS/DS/ES/FS/GS (64-bit: SS may be null at CPL0,
DS/ES/SS bases ignored, FS/GS base from the descriptor), MOV CR3, `pushq; pushq m; lretq` (far RET 64 → 64); decompressor
`startup_64`: MOV Sreg with null selectors, `lgdt`, `lretq`, `pushq $0; popfq`, `std; rep movsq; cld`; kernel `head_64.S`:
`wrmsr` (GS base), `lretq`, CR3/CR4/CR0 moves, `cpuid`, `lgdt`, MOV DS/SS, `popfq`.

**Not present on any path:** IN/OUT, IRET, far CALL, segment bases ≠ 0, non-4-GiB limits, A20, 16-bit code (except the
real-mode `trampoline_start`, used only by INIT/SIPI, which the mailbox avoids). Decision 1 holds.

## 3. Design

**Compiler (fexc).** `--profile slice1 --bitness 32` (refused today, `fexc.cpp:360`). FEX fixes the mode per context and
the server forks per request, so a request just carries the mode. FEX adds segment bases only for explicit prefixes in
32-bit code (`OpcodeDispatcher.cpp` `GetSegment`/`AppendSegmentOffset`, default prefix none), loading `ds_cached` etc. from
the frame. The allowlist becomes mode-aware: in 32-bit code cut CS/DS/ES/SS prefixes (`unsupported_form`), `63` (ARPL, not
MOVSXD), `60/61`, `27/2f/37/3f`, `d4/d5`, `d6`, `62`; `c4/c5` with a memory operand are LES/LDS (`system`); admit `40–4f`
(INC/DEC r32); the direct-branch target check wraps at 2³² instead of the canonical test; patch 0007's guard checks that
an access's last byte stays below 4 GiB (the flat limit; a crossing access is refused) instead of canonicality.
**FEX's 32-bit register map differs** (`Arm64Emitter.cpp` `x32::SRA` = x4,x7,x5,x6,x8–x11 + PF/AF in x26/x27, and
`x32::RA` allocates x12–x17, x19, x29, x30, x24 — the 64-bit homes of R8–R15): precision patch 3 follows FEX's
`StaticRegisters`, but `verify_precision.py`, admission and the resident's bank must use the per-mode map from
`state_abi`, and R8–R15 stay in the bank (never read back from native registers) while 32-bit code runs.

**Host.** Request mode from the bank; `--bitness` in the fexc args and the object-cache key; admission checks the
object's bitness (header byte 20) against the request and applies the mode's static map. Admission already refuses frame
loads outside `frame::FIELDS`, so a missed `*_cached` load cannot be admitted. `system.rs` decodes the new system forms in
both modes (32-bit ModRM: no RIP-relative, `mod 00 rm 101` = absolute disp32; operand size for LGDT/LIDT/far RET).

**Bank (`execution::Cpu`).** Add CS/SS/DS/ES/FS/GS (selector, base, limit, attributes), GDTR, IDTR, TR (selector and
cached descriptor; the IA-32e activation check reads its type), and the hidden PDPTEs (four entries plus a valid flag;
`State` stays ≤ 4 KiB). Derived: `code_mode()` = 64 / C / P (CS.D=0 or PE=0 → none: refused), and **flatness**: 32-bit
code requires flat CS, SS and DS (ES/FS/GS are used only by refused forms); a non-flat or null one is valid state that may
be loaded but makes the next dispatch stop `unsupported_mode` — refusal at execution, not at load.

**Walkers: `Cpu::control()` → `Cpu::paging() -> Option<Paging>`** with `Off` (identity resolver), `Legacy` (`paging32.rs`
plus CR4.PSE 4 MiB leaves with A/D on the PDE, PSE-36 bits 20:13 as PA 39:32, bit 21 reserved, SMEP), `Pae` (move
`families/pae.rs` `load`/`walk` out of `families`, drop its `Visit` instrumentation; the bank's hidden entries) and `Long`
(`paging.rs`, also for C with 32-bit linear addresses). One `walk(tables, paging, va, access)` serves the data walk, the
fetch walk and the descriptor/pointer reads. 32-bit linear range: no canonical test; an access whose last byte passes 4
GiB is refused (owner decision 1: linear wrap). **Paging-off resolver:** gpa = linear; RAM pages are write-back, writable,
executable, `dirty` true; never reads tables, writes A/D or raises #PF (contract 22); outside guest RAM → `Unsupported`
(device/hole deferred). INVLPG drops the 4 MiB range of a PSE leaf (today: 2 MiB).

**Code key.** `code_context` word: add the code mode (bit 2 today means 64-bit) and the paging kind. Far transfers change
the mode without a flush, so the mode bits are what keeps 64-bit and 32-bit blocks of one RIP apart (byte `40` is INC in
32-bit code and REX in 64-bit code); control changes already flush the whole cache. Links stay same-context.

**Hidden PDPTEs.** Loaded (read all four, then validate: present with bits 2:1, 8:5 or 63:40 set → #GP(0), nothing
published) on MOV CR3 in PAE paging, and on MOV CR0/CR4 that changes CD/NW/PG or PAE/PSE/PGE/SMEP when PAE paging
results (`pae::reload`); never on IA-32e activation, PDPT writes, INVLPG, WP changes or evictions (contract 29). Per vCPU.

**Resident transactions** (§2's checks): MOV CR0/CR3/CR4 and RDMSR/WRMSR in 32-bit modes (r32 operands, CR3 zero-extended
in C, PAE CR3 bits 4:0 ignored); PG on/off with IA-32e activation/deactivation; MOV Sreg (8E) all modes; LGDT/LIDT both
widths; far JMP (EA, FF /5 m16:32 and m16:64) and far RET (CB, CA iw, REX.W CB) at the same privilege; LTR (cheap once the
descriptor fetch exists; needed by the 64-bit boot path anyway). One descriptor-fetch helper: GDTR limit, read through
`paging()` (#PF with implicit supervisor access), A-bit set by compare-exchange through `CodeWrites` (a GDT on a code page
is a code write). Refused (`unsupported_system`): LDT selectors, gates/TSS as jump targets, far CALL, IRET, privilege-
changing returns, 16-bit targets, PE=0, LA57, MOV from Sreg, CPUID, PUSHF/POPF, LLDT (boot phase: none of these is on a
reached 32-bit path).

## 4. Chunks

Each keeps the slice-1/2/3 sets green (64-bit objects byte-identical: the 64 fixtures, seed 4242, control flow, faults,
ASM suite, S3c–S3e runners).

**S3g1 — 32-bit code on the general path (~1 run).** fexc 32-bit profile (allowlist, guard, precision with the x32
map, `verify_precision.py`/`check_sites.py`/`scan_skips.py` on 32-bit forms), host bitness/admission, bank segment state,
`code_mode()`, `paging()` over Off/Legacy(+PSE)/Pae/Long with the moved walkers, key bits, per-mode static map in enter
and bank. Driver lines: `mode long|compat|prot32`, `paging off|legacy|pse|pae|long` (the driver builds the tables and the
initial hidden PDPTEs), flat descriptor defaults. *Check:* (a) FEX's upstream `32Bit_ASM` tests through
`run_asm.py` (expected = FEX's `RegData`; `Mode: 32BIT` now supported) started in P0 (where the regions lie in guest RAM),
L, PSE, PP and C: pass or unsupported-with-reason, zero fail (245 files, but 138 are x87 and 32 VEX, so only ~75 exercise
the integer subset; S3g2 adds generated 32-bit programs run in C and P0 inside the QEMU wrappers below, or a 32-bit mode of
`ref.py` over `qemu-i386-static`, which the pinned QEMU user package already contains); (b) every passing program ends identical in all its paging
configurations (paging must be invisible); (c) a 32-bit paging fault matrix at CPL0 (WP 0/1, SMEP, NXE in PAE, reserved
bits, not-present, read-only, 4 KiB/4 MiB/2 MiB leaves) against an extension of `run_slice1_faults.Machine` to two-level
and PAE walks, cross-checked with `i4v_pae_model.walk` where it applies; (d) paging off: poisoned tables unchanged, no A/D,
no #PF; (e) explicit DS/ES/SS/CS prefix → `unsupported_form`, 4 GiB-crossing access refused, R8–R15 unchanged across 32-bit
code. *Lets S3i delete:* `i4v_off` (host driver, runtime/abi `paging_off32`, `i4v_off_check`/`worker`), the
`families/pae.rs` walker copy.

**S3g2 — mode/control/descriptor transactions and `startup_32`-like programs (~1 run).** §3's transactions and their host
decode. *Check:* QEMU system-mode programs on the S3r stub (bodies start in 64-bit CPL0 like S3c's; no stub change), at 1–4
CPUs and block sizes default/1: (1) `startup_32`-like round trip — `jmp far m16:32` to C, PG off, own GDT via LGDT, MOV
SS/DS/ES, paging-off loads/stores, build PAE+4-level tables with stores, CR4.PAE, CR3, WRMSR LME, CR0.PG, `ljmp ptr16:32` to
64, then compatibility code, then paging off again and back; (2) legacy two-level with a 4 MiB PSE page aliasing RAM at
0xC0000000; (3) legacy PAE (LME off) showing the hidden PDPTEs: rewrite a PDPTE in memory, the old root still translates,
MOV CR3 reloads; (4) `lretq`/`lretl` transitions shaped like `trampoline_32bit_src`. Bodies end with a 64-bit epilogue that
zero-extends the GPRs they used (upper halves are undefined after a mode switch, SDM vol. 1 §3.4.1.1), restore IDTR if
changed, and never load FS/GS (the stub's capture uses the GS base). Fault cases (QEMU 11.1.2 accepts or hangs on many
invalid writes, and its 64-bit IDT cannot record a 32-bit fault): `tools/system_fault_model.py` extended with 32-bit MOV
CR, activation, PDPTE, MOV Sreg, LGDT, LTR, far JMP/RET rules, evaluated at QEMU's state before the instruction, and
cross-checked with `i4v_modes_model` (activation, far-jump order) and `i4v_pae_model` (`load`, `needs_reload`) after the
inventory §3 items 14–15 fixes (EDX in WRMSR, declared Intel profile). *Lets S3i delete:* `i4v_controls` (driver,
`i4v_controls/producer.rs`, abi `families/{controls,modes,pae}`, runtime `families/controls/{long_mode,maintenance}`,
`i4v_controls_check`/`worker`; contracts 28–32).

**S3g3 — modes on N vCPUs (~0.5–1 run; mostly tests).** *Check (QEMU S3r, 2–4 CPUs):* mixed modes sharing a code page —
CPUs in 64, C, P0 and PP run the same physical page (bytes that mean different things per mode) and a shared data page;
one CPU patches the page and all see it at their next entry (S3d path); a paged CPU's PTE remap leaves a paging-off CPU's
routes unchanged (contract 27); concurrent legacy/PAE controls (contract 33): CPU 0 toggles CR3/PSE/PG and reloads its
PDPTEs while peers keep their own; coherence `ad-legacy`, `ad-pae`, `size-pae` re-expressed with `i4v_coherence_model`.
*Lets S3i delete:* `concurrent/mixed*` (`check_i4v_mixed`, `i4v_mixed32_worker`, abi `mixed32`, runtime
`concurrent/mixed.rs`), `concurrent/controls.rs` (+ `i4v_controls_concurrent_check`, abi `control_concurrent`, runtime
`controls/concurrent.rs`), the legacy/PAE coherence variants. `i4v_off_steps` and the address-form family go with a
retirement reason (decision 1); S3g1 tests their refusals.

## 5. Risks and owner questions

1. **Owner: the pinned path needs no 32-bit code.** With the planned loader (64-bit protocol), the mailbox AP start and
   LA57=0, Debian 6.12.107 executes no 32-bit or paging-off instruction (§1). Ubuntu 6.8's AP start does (compatibility
   mode, paging off, PAE/4-level, IA-32e activation, far jumps) — but never legacy two-level paging, PSE pages, legacy PAE
   paging or hidden PDPTEs: **no x86-64 Linux path enables those; only 32-bit guest kernels do.** Options: (a) as planned
   (~3 runs, retires every old family by coverage); (b) reduced (~2 runs): 32-bit profile, paging-off resolver,
   compatibility mode, IA-32e activation/deactivation, far JMP/RET, MOV Sreg, LGDT/LIDT/LTR, mixed 64/C/P0 modes; legacy
   two-level, PSE and legacy-PAE/PDPTE behaviour (contract 29, controls profiles 13–21, coherence `*-legacy`/`*-pae`)
   retired with the reason "not used by x86-64 Linux" and their walkers deleted (a PAE enable without LME then halts
   `unsupported_mode`). This changes the phase boundary's "paging modes" coverage, so it is the owner's call.
2. **The real 32-bit route needs more than S3g.** Ubuntu's `pa_trampoline_compat` takes a **LOCK BTS** spin lock (not
   admitted; S3e put LOCK BT* in the boot phase), and its CR4/CR0 writes carry Linux's feature bits (`mmu_cr4_features`:
   PGE, PSE, OSFXSR, OSXMMEXCPT, …; `CR0_STATE` = PE|MP|ET|NE|WP|AM|PG), refused today. S3g programs use XCHG locks and
   plain values; accepting CR0 MP/NE/AM as state is cheap and proposed for S3g2; CR4 feature bits stay boot phase.
3. **FEX 32-bit mode is unexercised under the slice-1 patches.** Different static map and allocatable registers (x29, x30,
   x24 and R8–R15's homes), 32-bit address truncation forms in site records, inc/dec and other 32-bit-only opcodes. If the
   precision verifier or the guard patch needs real patch work, S3g1 grows to ~2 runs.
4. **Reference gaps.** QEMU system mode cannot record faults taken in 32-bit modes with the stub's 64-bit IDT, and pinned
   QEMU mishandles invalid control writes, so every fault case relies on the SDM models. Whether pinned QEMU sets a
   descriptor's accessed bit on segment loads and far transfers as the SDM requires is unverified: S3g2 programs use
   descriptors with A preset (as the kernel's GDTs mostly do) and test the A write against the model first.
5. **Not covered, by design:** 32-bit CPL3 code (needs privilege-changing returns/IRET: boot phase or 32-bit userspace),
   CPUID, PUSHF/POPF, CLI/STI, LLDT, REP string ops and the decompressor's 32-bit boot protocol path (§2 C) — needed only if
   the loader moves to the 32-bit protocol. No reached path needs segment bases, A20 or 16-bit code (decision 1 stands).
