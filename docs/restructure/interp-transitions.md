# The interpreter's resident operations and mode transitions

Status: built, November 22, 2026 (M2 Part 2, on `boot-interp` 897f201c, e21b0209 before the branch's rebase onto main;
plan sections 5 and 6 are the spec). It completes [the interpreter in the resident](interp-integration.md): the
protected-mode far JMP and far RET, MOV Sreg, MOV to CR0/CR2/CR3/CR4 with the mode transitions, HLT, and the hand-off to
the translator; and the translator's way into the interpreter from 64-bit kernel code (`refuse_compatibility`'s CPL-0
arm).

## Where the code is

- **Rules, pure, in `crates/abi/src/interp/system.rs`** (host unit tests): the descriptor-table entry of a selector, the
  checks of a far JMP target, a far RET target and a segment load, MOV CR0 and MOV CR4. Each takes the bank and the
  operands (the raw descriptor once read) and returns the new segment or value, or a `Check`: an exception (vector,
  error code) or a refusal (a typed stop). Order and outcomes are `segment_model.jmp_far_interp`, `ret_far_interp`,
  `load_interp` and `modes_model`'s, with the rules outside 64-bit mode: L ignored while LMA = 0, a null SS #GP(0).
- **Memory and commit in the resident, `execution/interpret/transitions.rs`** (a child of `interpret`): the descriptor
  read and the accessed-bit write are implicit supervisor accesses through the interpreter's memory layer (`Mem`: the
  identity with paging off or the compatibility-mode walk, guest RAM, unassigned memory, the device stop; plan section
  4), then the commit and the resident's consequences. `segments::descriptor` and `accessed` are not called: their
  memory goes through `segments::pages`, whose walk controls (`controls()`) exist only in the states translated code
  runs. `far_target` is not called either: it halts on a CPL-0 call gate, refuses L = D = 1 whatever LMA and gives
  #GP(selector) for task gates and TSSs. Shared with the 64-bit functions: the descriptor layout (`Segment::loaded`) and
  the CS load with its accessed bit (`segments::load_cs`). `same_mode`, `far_transfer`, `far_return`, `load_data` and
  `far_target` are not edited.

## The operations

| Operation | Checks (in order), each a typed stop `interp_fault` with nothing committed | Commit |
| --- | --- | --- |
| far JMP, protected mode (ptr16:16, ptr16:32, m16:16, m16:32; the core has read and checked the operand) | null #GP(0); table limit (TI = 1: LDTR present) #GP(sel); a call gate, or with LMA = 0 a task gate or an available TSS: `interp_unsupported` GATE; other system (a busy TSS among them) or data #GP(sel); LMA and L = D = 1 #GP(sel); privilege #GP(sel); P #NP(sel); offset past the new limit (not into 64-bit code) #GP(0); the accessed-bit write (#PF) | CS (RPL = CPL), EIP, RF clear |
| far RET 16/32, imm16, protected mode (the core has checked and read both slots) | null #GP(0); table limit; not code, or LMA and L = D = 1 #GP(sel); RPL < CPL, DPL #GP(sel); P #NP(sel); RPL > CPL: `interp_unsupported` OUTER; offset past the limit #GP(0); the accessed bit | ESP (or SP, by SS.B) released, CS, EIP |
| MOV Sreg, protected mode | null: SS #GP(0), else the unusable segment (FS/GS base 0); table limit; `load_data`'s type, DPL/RPL and P rules (#GP/#SS/#NP(sel)); the accessed bit | the segment, EIP; no MOV SS shadow |
| MOV CR0 | value & CR0_VALID \| ET; PG without PE, NW without CD #GP(0); PG 0 -> 1: without EFER.LME `interp_mode` PAGING (legacy or PAE paging); without CR4.PAE, with CS.L = 1 or a 16-bit TSS in TR #GP(0), else LMA := 1; PG 1 -> 0: LMA := 0 (64-bit mode never interprets) | CR0, EFER |
| MOV CR4 | a bit the CPU profile does not support (`CR4_VALID`: LA57, PCIDE, SMAP among them; MCE is valid without CPUID's MCE, a recorded deviation) #GP(0); PAE changed while LMA = 1 #GP(0) | CR4 |
| MOV CR3, CR2 | (none for a 32-bit value) | CR3 with `stack_traps` 0 and `x87::address_space`; CR2 |
| HLT | (CPL 0 only) | EIP past it, then `events::park`: an INIT wakes it (the next boundary takes it), an NMI or an enabled interrupt wakes it for `interp_event`, a SIPI changes nothing |

A descriptor-table entry above 4 GiB in compatibility mode (the memory layer is 32-bit): `interp_unsupported` TABLE (none
of the surveyed stubs). `stop::interp` details: GATE 6, OUTER 7, TABLE 8 (`interp_unsupported`), PAGING 1 (`interp_mode`);
the "not built yet" details 1-5 are gone. `stop::INTERP_BUG` (86, `interp_bug`): an interpreter invariant broken (an
access no check covered, a state it cannot key), which halted as `bad_reply` before.

## What the resident resets at each transition

The interpreter uses no shadow, no L1 and no translated block; its records are keyed by mode, CPL and CS.D and checked by
their bytes, so mode changes need nothing for them. What translated code sees after the hand-off is kept exact as the
64-bit transaction keeps it:

- R <-> P16/P32 (PE): nothing (no walk changes).
- PG 0 -> 1 with LME (P -> C, LMA set), PG 1 -> 0 (C -> P, LMA clear), a CR0.WP change, a CR4 paging bit (PGE, PAE,
  SMEP): `tlb(s, true)`, the global flush (every shadow, L1 entries and links of blocks no longer current).
- MOV CR3: `tlb(s, false)`, as the transaction (a load of the same value too).
- EFER through WRMSR (the existing transaction): its global flush on LME/LMA/NXE.
- CR0 TS/EM/MP/NE/AM/CD/NW, CR4 OSFXSR and the other feature bits: nothing (in the code context, or not modelled).

The `tests/smp` program `seg-compat-tlb` checks the CR3, CR0.PG and CR4 flushes, each alone (Cases, below).

## The hand-off and the way in

- **Out:** a far JMP or far RET that loads a 64-bit CS (L = 1) with LMA = 1 completes in its pass; the loop's next top
  finds `interpreted_mode()` = None and returns to `find_from`'s loop (through `boundary`) or to `start`'s dispatch. RIP
  is the 32-bit offset; the GPRs' upper halves are what 32-bit writes left (zero after INIT). With LMA = 0 the L bit is
  ignored: such a CS is 16- or 32-bit code and stays interpreted.
- **In (`refuse_compatibility`'s CPL-0 arm):** the halt moved into the cold `interpret::kernel_compatibility`, which
  returns for a CPL-0 target with L = 0: the transfer completes in `far_transfer`/`far_return`, and `commit`'s
  `ends_in_mode` records the interpreted state. It halts `unsupported_code_mode` as before for L = D = 1, a return to
  CPL 1 or 2, and any IRET (the instruction's opcode after its prefixes: an IRET into compatibility mode may load a null
  SS, which x86 refuses with #GP(0) and the 64-bit IRET does not check; no known code makes one). The CPL-3 arm is
  untouched. `refuse_compatibility` is kept out of line (`#[inline(never)]`, as the compiler had left it), so its
  callers are the same code, and its CPL-0 arm is a tail call of `kernel_compatibility` (which returns its result), so
  its common path keeps no frame.
- **A deviation there:** `far_transfer` and `far_return` check only that the target RIP is canonical (and stay as they
  are), where x86 raises #GP(0) at the instruction for a compatibility-mode target outside the new CS limit (a RIP at or
  above 4 GiB, or past a smaller limit). The cold `interpret::record` (before `commit` cuts RIP to 32 bits) stops such a
  transfer once it has completed: `unsupported_code_mode` with `native[0]` = `stop::code_mode::PAST_LIMIT`, the limit
  and RIP (the host's report names the case), the bank the state at the target. A loud stop where x86 gives #GP(0), in
  place of the code running at the wrong address; no known code does this. `segment_model`'s 64-bit `far` and `ret`
  give it as `Stopped` (a refusal at the target, with x86's #GP(0) in `x86`).

## The driver's `init` event source

The program driver's event sources (`post`, `nmi`, and now `init ADDR`) post their event when the interpreter asks for
the decode at ADDR and then let the instruction there run as itself (the host's `REQUEST_DECODE` handler took a hook's
system answer for an error before): so the boundary after that instruction is the first to see the event. It states
the INIT-before-TF order and the HLT wakes deterministically; machine descriptions accept the three items.

## Cases (M2 Max)

- abi unit tests (`natemu_abi::interp::system::tests`): table entries, far JMP and far RET targets, segment loads, MOV
  CR0/CR4, against the models' outcomes.
- `tests/interp` case `natemu`: all 1,716 corpus cases match their locked answers (`natemu-pending.txt` removed).
  `R-mov_cr4,_r32-02` and `P32-mov_cr4,_r32-02` write CR4 = 750H, whose MCE bit natemu's CPU profile does not advertise:
  for a while they wrote 710H (#GP(0) in natemu, where QEMU's and KVM's CPU models load it); since CR4.MCE is accepted
  (a recorded deviation, `cpu_profile.json` `control.cr4.bits.MCE`) they write 750H again, judged by their original
  answers. In 64-bit kernel code, `tests/smp` `sys-cr4-mce` (MCE set and cleared, read back, against QEMU).
- `tests/interp` case `natemu-faults` (`interp_faults.py`): 306 segment cases, `segment_model`'s far JMP (ptr16:16,
  ptr16:32, m16:32), far RET (16, 32, imm16) and MOV Sreg in R, P16, P32 and C32 over a GDT covering every check (busy
  32- and 16-bit TSSs among them; with not-present operand, stack and descriptor pages and a read-only GDT page under
  CR0.WP in C32), each judged by the model's result: the typed stop and its record with nothing committed (every field
  and the GDT's bytes as they started), a refusal's detail, or the state the model reaches (the stop at the target, the
  selectors, ESP, the GDT's accessed bits); and 21 of `modes_model`'s programs (profiles 22-49 but 24, 28, 30, 34, 35, 36, 38, whose endings are
  64-bit code's or need a 16-bit TSS) run whole, ending where the model's records first fault or stop (or at the end),
  with the model's CR0, CR3, CR4 and EFER there. Their 64-bit stretches run translated, through the hand-off and the
  CPL-0 arm.
- `tests/interp` case `natemu-machine` adds: an INIT posted at the POPFD that sets TF is taken first; HLT woken by an
  NMI (the typed stop, vector 2) and by an INIT; HLT alone ends the run halted.
- `tests/system` case `initsipi-interpreter`: every scenario but `bsp-init`; `fixed-in-wfs`'s own start program (the
  climb, the APIC enabled, STI, an idle HLT) through a jump the BSP patches at the stub's 64-bit code.
- `tests/boot` case `exit-ap-natemu`: the pass at 1, 2 and 4 vCPUs with the interpreter (each AP climbs and arrives
  once, counted from 64-bit code).
- `tests/boot` case `kut-natemu`: kvm-unit-tests' smptest and dummy at 1 to 4 vCPUs through the Multiboot loader and
  `--debug-exit`, judged by `run_kut.judge` and against the locked KVM answer's lines (all eight pass; with
  `NATEMU_INTERPRETER=off` all fail).
- `tests/smp` case `system`: `seg-jmp-far-compat` and `seg-lret-compat`, refusals until now, are round trips judged
  against QEMU system mode (64-bit code into the CPL-0 32-bit segment 38H, EAX set there, back by a far JMP m16:32 or a
  32-bit far RET); new `seg-iret-compat-cpl0` (IRETQ there: still `unsupported_code_mode`). `segment_model`'s 64-bit
  `far`/`ret` follow (`_refuse_compatibility` completes CPL 0 with L = 0 but IRET; `_refuse_shape` only at CPL 3). The
  CPL-3 lock cases are unchanged and pass. `seg-jmp-far-compat-rip` (JMP m16:64 to 38H at 1_0011_0000H),
  `seg-lret-compat-rip` (RETQ there) and `seg-call-far-compat-limit` (CALL m16:64 to a 32-bit CPL-0 segment of limit
  FFFFFH at 110000H): the deviation's stop at the target (`segment_model`'s `Stopped`). New `seg-compat-tlb`, judged
  against QEMU system mode (registers, captured memory): 64-bit code reads V (its shadow made), goes to 38H, changes V's
  translation there and reads V back in 64-bit code: (1) CR3 := a second table that maps V elsewhere, (2) V's PTE
  rewritten without INVLPG, then CR0.PG cleared and set (IA-32e mode off and on, P32 with paging off between), (3) the
  same with CR4.PGE set and cleared; CR0, CR4 and EFER read back in 64-bit code.
- Static case `stack`: the gate bound is 21,792 of 81,920 bytes (26,128 with one nested entry), its deepest path now
  through the interpreter into a 64-bit far transfer's `kernel_compatibility` (the static over-approximation of
  `transaction` inlined into `run`; `refuse_compatibility`'s frame is 0 since the tail call); boot 11,904 of 32,768.

Each new check was shown to fail when it should, by a temporary mutation of each: the INIT-before-TF order
(`p32-init-before-tf`), L = D = 1 under LMA (the C32 far JMPs and RETs to 58H), the accessed-bit write (the far
transfers and loads of 30H and A0H), LMA cleared on deactivation (the round-trip programs), the CPL-0 arm (the modes
programs through it), HLT's wait (`r-hlt-halted`), and an interrupt delivered in `fixed-in-wfs`'s idle. After the
review: the CR3, the CR0 and the CR4 flushes of `mov_to_cr`, one run of case `system` each (`seg-compat-tlb` fails at
both block sizes, every other program passes); the past-the-limit stop (the three cases above) and the busy-TSS rule
(the eight P16 and P32 far JMPs to B0H and B8H), in one run.

## The hot paths (resident release builds, `llvm-objdump`, addresses, symbol hashes and literal-pool addresses aside)

Experiment (not a framework case; M2 Max): the disassembly and the resident's sizes below, on the commits before the
branch's rebase onto main (tag `boot-interp-prerebase`; [the commit map](commit-map-boot-interp-2026-11-22.txt) gives
their new ids).

Against b5266dc4 (this change on deea8131), identical: `find_from`, `sys_fast`, `miss_fast` (both with `sys_commit`
inlined), `system` (with `transaction`), `commit`, `set_cpl`, `far_transfer` (with `same_mode` and `far_target`),
`far_return`, `load_data`, `trap_due`, `segments::pages`, `linear`, `copy`, `descriptor`, `accessed`, `translate`,
`fetch_walk`, `enter`, `step_here`. `refuse_compatibility`'s common path (a 64-bit target: every 64-bit far JMP, CALL
and RET and every IRETQ to 64-bit code, `sys_commit`'s fast IRET among them) is `and`, `mov`, `cmp`, `b.ne`, `str`,
`ret`: 6 instructions, no frame (b5266dc4: the same test, then `mov`, `b`, `str`, `ret`). Its CPL-0 arm is `b
kernel_compatibility`, a tail call (the cold function returns `refuse_compatibility`'s result), where a copy of `State`
and a halt were. Without `#[inline(never)]` the smaller function was inlined into `far_transfer` and `far_return`, which
then differed. The first version called `kernel_compatibility` and returned `Ok(())` after it: the common path kept a
frame (`stp x30, x19`, `mov x19, x8`, `ldp`), 11 instructions (the review's finding).

The first version also moved three functions with no source change through the code-generation units' partitioning:
`helpers::x87` called an outlined `x87::helper` (one more call per `svc #X87`), and `mmio::ones` and
`maskmov::transaction` differed in frame and register allocation. On deea8131 `#[inline]` on `x87::helper` (its one
caller) restored all three; on `boot-interp` d8049820, whose firmware mode moved the partitioning again, it restores
`helpers::x87` and `xsave::transaction` but changes `sse::bank`, which every exit's bank runs, so it is not taken:
`helpers::x87` keeps the outlined call. Against d8049820 this change's code differs only in `refuse_compatibility`,
`kernel_compatibility`, `interpret::record` and `natemu_abi::interp::system::jump`. Resident text: 261,516 (b5266dc4)
-> 267,440 bytes on deea8131; 269,780 (d8049820) -> 269,796.

## Risks for a running 64-bit OS

- `refuse_compatibility`'s CPL-0 arm: a CPL-0 far JMP, CALL or RET into compatibility-mode code, which halted before,
  now enters the interpreter (Ubuntu 6.8's AP route, Linux's LA57 switch, kvm-unit-tests' 5-level tests; the LA57 switch
  itself still stops, CR4.LA57 being reserved in the profile). CPL-3 transfers keep their #GP deviation.
- Everything else runs only from interpreted state.
- A 64-bit far transfer into CPL-0 compatibility-mode code with RIP outside the new CS limit stops loudly after it
  completes, where x86 raises #GP(0) at the instruction (the recorded deviation above; no known code does this).
- The resident grows by 5.8 KB (the experiment above); the interpreter's gate path deepens (the static `stack` case).
