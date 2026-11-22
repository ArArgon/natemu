# The boot-time interpreter's per-instruction cases: format and references

Status: November 22, 2026. The reference side runs and is locked (`tests/interp`, case `references`); natemu does not
run these cases yet: its program driver starts only in 64-bit long mode (`crates/driver/src/drivers/program.rs`). This
page is the contract between the generator, the reference rig and the natemu side still to come (M2 Part 2, plan
section 8, "Per-instruction cases in each mode").

## What there is

| File | What it is |
| --- | --- |
| `tests/interp/interp_cases.py` | The generator and the format's definition: `corpus()` gives the document below. Deterministic (seed 20261122). |
| `tests/interp/interp_stub.asm` | The mode-setup stub: a one-CPU variant of the SMP stub (`tools/bench/smp/stub_smp.asm`), a Multiboot image. |
| `tests/interp/interp_rig.py` | The capture blobs, the image (stub + case table), the two references, parsing, each case's answer. |
| `tests/interp/run_interp_ref.py` | The `references` case: the checks below, from the lock (or, seeding, from the references). |
| `tests/interp/tests.lock` | Every case's two answers, inline (`[answers]`, `tools/reference/answers.py`). |

The corpus covers exactly section 1's first-version list in R, P32 and C32, the P16 stretch, and the transitions R → P16,
R → P32, P16 → R, P32 → C32, P32 → 64-bit, C32 → P32 and C32 → 64-bit: 1,716 cases (case `references`, the generator at
seed 20261122). HLT is not among them: with IF = 0 nothing ends it, so nothing after it can be observed; the INIT and
SIPI cases cover it. Faulting forms are not either: the fault cases are separate (plan section 8).

## The case document

One JSON document (`interp_cases.py -o cases.json`); numbers are hex strings (`"0x1f"`), byte strings plain hex.

```
{ "format": "natemu-interp-cases-v1", "generator": "tests/interp/interp_cases.py", "seed": 20261122,
  "common": [ {"name": "gdt", "addr": "0x1000", "data": "..."}, ... ],
  "cases": [ { "name", "group", "form", "mode", "note", "state", "code", "memory", "stops", "expect_stop", "compare",
               "rig", "hash" }, ... ] }
```

**`common`:** memory every case starts with, besides its own: the GDT at 1000H (13 descriptors; 00H-28H are the stub's,
the rest the cases', some with the accessed bit clear), its copy at 8000H (an LGDT target), and two identical sets of
4-level tables at 90000H and 94000H (identity, supervisor, writable, A and D preset so no walk writes them: 0-2 MiB in
4 KiB pages, 2 MiB-1 GiB in 2 MiB pages; C32's CR3 and a MOV CR3 target).

**`mode`:** where the case starts: `R` (real mode), `P16` (CR0.PE set, CS still the real-mode cache: the stretch before
the far jump), `P32` (protected mode, paging off, CPL 0), `C32` (IA-32e active, CS a 32-bit code segment, CPL 0).

**`state`:** the whole architectural state at the case's first instruction:

| Field | Meaning |
| --- | --- |
| `gpr` | EAX ECX EDX EBX ESP EBP ESI EDI, 32 bits (the upper halves of the 64-bit registers are 0) |
| `eip`, `eflags`, `cpl` | EIP (CS-relative), EFLAGS (bit 1 set; TF, RF, VM clear), CPL (always 0) |
| `seg.{es,cs,ss,ds,fs,gs}` | `sel`, `base`, `limit` (byte granular), `attr`: natemu's `Segment` exactly (`crates/abi/src/execution.rs`: descriptor bits 47:40 in 7:0, bits 55:52 in 15:12) |
| `cr0`, `cr3`, `cr4`, `efer` | the control registers; C32's EFER has LME and LMA |
| `gdtr`, `idtr` | `base`, `limit` |

Hidden parts: R and P16 hold real-mode caches (`base` = `sel` × 16, limit FFFFH, attributes 9BH for CS and 93H for the
others: the stub's 16-bit descriptors, which a real-mode load keeps); P32 and C32 hold what the selector's descriptor in
`common`'s GDT gives, accessed. LDTR and TR are not used (the reset values). DR7 is 0; CR2, the x87 and SSE state are not
part of a case.

**`code`:** `addr` (linear 10100H in every case), `data` (the instruction bytes), `asm` (their source). It is also in
memory: the driver writes it like any other block.

**`memory`:** the case's own blocks (`addr`, `data`), physical. Paging is off in R, P16 and P32, and C32's tables map
identically, so linear = physical everywhere. A block never overlaps another, `common` or a stop; nothing lies in
A0000H-FFFFFH or below 10000H outside `common`. A case reads only memory it states.

**`stops`:** where the case ends: `id`, `linear`, `cs` (selector), `eip`, `mode` (the mode there: `R`, `P16`, `P32`,
`C32` or `L64`, 64-bit mode). Stop 0 is the end of the code (the fall-through); the others are a case's landings (far
transfers, near RET, transitions). natemu stops when CS.base + EIP reaches a stop's `linear`, checked before every
instruction, the first included (an empty case stops before it runs anything); which stop it reached is the `stop` field
of its answer. `expect_stop` is the one the case means (the check below holds both references to it).

**`compare`:** what is compared at the stop, and who answers it:

| Field | Meaning |
| --- | --- |
| `gpr.<reg>` | mask per register (all 32 bits; in 64-bit mode the low 32 bits of RAX..RDI) |
| `eflags` | mask: 3C7FD7H (every defined bit but RF and VM) less the flags the case's instructions leave undefined (SDM Vol.2 "Flags Affected": AF after a logic operation, OF and AF after a shift by more than one, CF after a shift by at least the width, OF SF AF PF after the bit tests, the six arithmetic flags after MOV to or from a control register) |
| `cr0`, `cr3`, `cr4`, `efer` | masks: CR0's defined bits (E005003FH), all of CR3, CR4 VME..OSXMMEXCPT (7FFH), EFER SCE LME LMA NXE (D01H) |
| `windows` | memory compared at the stop (`addr`, `len`): the data window (20100H, 64 bytes), ES/FS/GS windows (16 bytes each at 30100H, 40100H, 50100H), the stack window (60FE0H, 64 bytes), and the case's own (a descriptor whose accessed bit a load sets, a store through a new segment base) |
| `by` | a field one reference answers alone: `qemu`, `kvm` or `profile`; absent means both |

The answer fields: `stop`, the eight GPRs, `eflags`, the six selectors, `cr0`, `cr3`, `cr4`, `efer`, `gdtr` and `idtr`
(`[limit, base]`), `window0`... (hex). Hidden segment parts are not answer fields: no reference can read them; a case
that loads a segment register shows its base through an access (a store through it lands in a window).

`by` is how vendor and emulator differences are settled (plan section 8):

- `profile`: CPUID's four outputs and RDMSR IA32_MISC_ENABLE's EDX:EAX are the CPU's own values; natemu's are its
  profile's (`tools/models/cpu_profile.json`): natemu's side is to compare those with the profile, not with a
  reference (no such check exists yet). Every other field of those cases is compared.
- `kvm`: a far transfer into a code descriptor whose accessed bit is clear sets the bit (SDM Vol.3 3.4.5.1, and AMD
  does); QEMU's TCG does not (11.1.2 and Debian's 10.0.13: 13 cases differ there), so the GDT windows of the
  protected-mode far transfers (45 cases) are the hardware's. A data segment load sets it on both.
- `qemu`: for an AMD answer that differs from the Intel CPU natemu presents. None so far: on everything else the two
  references agree.

**`rig`:** reference-only bytes and entry values, which natemu's driver ignores: each stop's capture blob (at the stop's
linear address), P16's prologue (below), `entry_eip` and `entry_esp`.

**`hash`:** sha256 of the case without its name, group, form and note. An answer's lock key is this hash, the
reference's name and `rig_digest` (`interp_rig.py`): the stub, the capture blobs, `common` and the reference's identity
(QEMU's pin, CPU model and arguments; KVM's CPU flag, arguments, container image and snapshot). A renamed case keeps its
answers; a changed case, stub, common block or reference needs new ones.

## What natemu's side is to do (integration, after the driver's start states)

For each case: write `common` and the case's `memory` and `code`, set `state` directly (mode from CR0, EFER and CS's
hidden part; the six segment registers with their hidden parts; GDTR, IDTR; RIP = `eip`; nothing else running), run
until CS.base + EIP reaches a stop, checked before each instruction (or the interpreter stops with a typed stop: a
failure), and compare per answer field:
`by` absent → the locked QEMU answer (equal to KVM's), `qemu` or `kvm` → that one, `profile` → not here. The masks
apply to natemu's values the same way (`interp_rig.answer` is the projection: give it natemu's record). In 64-bit mode
(stop mode `L64`) compare the low 32 bits of the GPRs.

The program driver (`program.rs`) needs, per mode: the six segments with hidden parts (today `flat_segments(cpl)`), RIP
and the 32-bit GPRs, EFLAGS with IF, CR0, CR3, CR4, EFER as given (today PE/PG/WP and LME/LMA are derived), GDTR and
IDTR, physical memory blocks (today it builds its own page tables: C32 uses `common`'s instead), and stops at linear
addresses (today: the end of the body holding the RIP).

## The references and the rig

The references (`interp_rig.py`): `qemu` is the pinned QEMU 11.1.2 system emulator (TCG) the framework builds, with the
system CPU model the SMP reference uses (`max,-pcid,-smap`); `kvm` is the x86 reference machine (an AMD Ryzen 7 8845HS)
under KVM with `-cpu host,-pku`, Debian trixie's QEMU at the campaign snapshot in the builds' pinned container, as the
framework's other KVM reference boots: the instructions run on the processor (real mode and P16 natively: B0's
spike). That machine is itself a KVM guest: the case code still runs on the hardware, through nested SVM.

One image holds every case. The stub (`interp_stub.asm`) is entered by Multiboot in 32-bit protected mode, its home.
For each case it copies `common` and the case's blocks into place, loads the case's state and jumps to its first
instruction:

- R: through 16-bit protected code (64 KiB limits), PE cleared, then a real-mode routine loads CR4, EFER, CR3, GDTR, IDTR,
  CR0, EFLAGS (on the stub's stack), the segments, the GPRs and DS last, and far-jumps to CS:IP.
- P16: as R, but the final far jump enters a 14-byte prologue just before the case's first instruction, in the case's own
  code segment: EAX's value, `mov cr0, eax` (PE: the CS cache stays the real-mode one), `popfd` (the case's EFLAGS, which
  its memory holds just below its SP: those 4 bytes are case memory, so natemu starts with them too), `mov eax, [cs:slot]`.
- P32, C32: the control registers (C32: PAE, CR3 and LME before PG, so CR0 activates IA-32e mode in the stub's 32-bit code
  segment), GDTR, IDTR, EFLAGS, segments, GPRs, DS, far jump.

Each stop is a capture blob for its mode: it saves EAX, ESP and SS in DR0-DR2 (no memory touched), loads a base-0 SS
it knows, takes EFLAGS with PUSHFD on a stack of its own, stores everything (selectors, CR0, CR3, CR4, EFER, SGDT, SIDT)
into a record at 6F00H and goes home (64-bit mode stores directly: it ignores data segment bases). Home (EFLAGS, CR0,
CR4, EFER reset) prints the record and the windows on COM1 as one line; an exception anywhere exits QEMU through
isa-debug-exit with 40H | vector. The rig checks itself: the empty cases (group `Rig`, all four modes, with every flag
pattern a case starts with) must read back their starting state exactly.

One reliance on a processor's choice: the SDM leaves the six arithmetic flags undefined after MOV to or from a debug
register, and the capture's three MOV DRn come before its PUSHFD (a case may leave no usable stack, so nothing can be
saved earlier without touching its memory). Both references keep the flags (QEMU's MOV DR never writes EFLAGS; the AMD
machine agrees with it on every case), and the empty cases check it at every seeding, with all six flags set, all clear
and mixed, in every mode: a reference that changed them would fail there.

## The checks (case `references`) and seeding

`tools/natemu-test run tests/interp`: no VM starts. The corpus is generated; per case both locked answers must exist,
agree on every field both answer, stop at `expect_stop`, and an empty case's must be its starting state. A changed case
has a new hash and no answer: the case reports it as not seeded.

Seeding: `TESTKIT_SEED_ANSWERS=1 NATEMU_X86_HOST=ubuntu@192.168.12.11 tools/natemu-test run tests/interp`, twice (the two
runs must agree: the lock refuses answers that differ), then `tools/natemu-test lock tests/interp --from-run RUN1 RUN2`.
QEMU runs on the Mac through the framework (seconds), KVM on the x86 machine in a container that is removed afterwards
(about a minute with QEMU's installation). Each run keeps both COM1 outputs and the KVM machine's description (CPU,
kernel, QEMU, virtualization) in the case's output folder.
