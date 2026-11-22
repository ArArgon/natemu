# The interpreter's resident operations and mode transitions

Status: design, November 22, 2026 (M2 Part 2, on `boot-interp` e21b0209; plan sections 5 and 6 are the spec). Nothing
built. It completes [the interpreter in the resident](interp-integration.md): the operations that stop
`interp_unsupported` today (protected-mode far JMP/RET, MOV Sreg, MOV CR, HLT) and the hand-off to the translator.

## Where the code goes

- **Rules, pure, in `crates/abi/src/interp/system.rs`** (host unit tests, plan section 8): the descriptor-table entry of
  a selector, the checks of a far JMP target, a far RET target and a data-segment load, MOV CR0/CR3/CR4. Each takes the
  bank and the operands (the raw descriptor once read) and returns the new value or a `Check`: an exception (vector,
  error code) or a refusal (a typed stop). Order and outcomes are `segment_model.jmp_far_interp`, `ret_far_interp`,
  `load_interp` and `modes_model`'s, with the non-long-mode rules: L ignored when LMA = 0, a null SS #GP(0).
- **Memory and commit, in the resident (`execution/interpret/transitions.rs`, a child of `interpret`):** the
  descriptor read and the accessed-bit write are implicit supervisor accesses through the interpreter's memory layer
  (`Mem`: identity or the compatibility-mode walk, RAM, unassigned, device stop; plan section 4 says implicit accesses
  go through that path), then the commit and the resident's consequences. `segments::descriptor` and `accessed` are not
  called: their memory goes through `segments::pages`, whose `controls()` halts outside 64-bit or CPL-3 state, and
  `copy`/`atomic_byte` are the firmware agent's this round. `far_target` is not called either: it halts on a CPL-0 call
  gate, refuses L = D = 1 whatever LMA, and gives #GP(selector) for task gates and TSSs (the model: a task switch,
  refused). `fields`'s layout, `Segment::loaded` and `load_cs`'s accessed-bit rule are reused. None of the five
  functions the plan keeps byte-identical changes for its callers.

## The operations

| Operation | Checks (in order), each a typed stop `interp_fault` with nothing committed | Commit |
| --- | --- | --- |
| far JMP, protected mode (ptr16:16, ptr16:32, m16:16, m16:32; the core has read and checked the operand) | null #GP(0); table limit (TI = 1: LDTR present) #GP(sel); a call gate, or with LMA = 0 a task gate or TSS: stop `interp_unsupported` GATE; other system or data #GP(sel); LMA and L = D = 1 #GP(sel); privilege #GP(sel); P #NP(sel); offset past the new limit (not for a 64-bit CS) #GP(0); accessed-bit write (#PF) | CS (RPL = CPL), EIP, RF clear |
| far RET 16/32, protected mode (the core has checked and read both slots) | null #GP(0); table limit; not code, or LMA and L = D = 1 #GP(sel); RPL < CPL, DPL #GP(sel); P #NP(sel); RPL > CPL: stop OUTER; offset past the limit #GP(0); accessed bit | ESP released by SS.B, CS, EIP |
| MOV Sreg, protected mode | null: SS #GP(0), else the unusable segment (FS/GS base 0); table limit; `load_data`'s type, DPL/RPL, P rules (#GP/#SS/#NP(sel)); accessed bit | the segment, EIP |
| MOV CR0 | value & CR0_VALID \| ET; PG without PE, NW without CD #GP(0); PG 0 -> 1: without EFER.LME stop `interp_mode` PAGING (legacy or PAE paging); without CR4.PAE, with CS.L = 1 or a 16-bit TSS in TR #GP(0), else LMA := 1; PG 1 -> 0: LMA := 0 (64-bit mode never interprets) | CR0, EFER |
| MOV CR4 | reserved bits (the profile's CR4_VALID) #GP(0); PAE changed with LMA #GP(0) | CR4 |
| MOV CR3 (CR2) | in IA-32e mode bits past MAXPHYADDR #GP(0) (none in a 32-bit value) | CR3, `stack_traps` 0, `x87::address_space` |
| HLT | (CPL 0 only) | EIP past it, then `events::park`: INIT wakes it (the next boundary takes it), an NMI or an enabled interrupt wakes it for the typed stop `interp_event`, a SIPI changes nothing |

A descriptor table above 4 GiB in compatibility mode (the memory layer is 32-bit): stop `interp_unsupported` TABLE
(none of the surveyed stubs). New details in `stop::interp`: GATE, OUTER, TABLE (`interp_unsupported`), PAGING
(`interp_mode`); FAR_JUMP .. HLT stay as numbers, unused.

## What the resident resets at each transition

The interpreter uses no shadow, no L1 and no translated block; its records are keyed by mode, CPL and CS.D and checked by
their bytes, so mode changes need nothing for them. What translated code will see after the hand-off is kept exact as
the 64-bit transaction keeps it:

- R <-> P16/P32 (PE): nothing (no walk changes).
- PG 0 -> 1 with LME (P -> C, LMA set), PG 1 -> 0 (C -> P, LMA clear), a CR0.WP change, a CR4 paging bit (PGE, PAE,
  SMEP): `tlb(s, true)`, the global flush (every shadow, L1 entries and links of blocks no longer current).
- MOV CR3: `tlb(s, false)`, as the transaction (a load of the same value too).
- EFER through WRMSR (the existing transaction): its global flush on LME/LMA/NXE.
- CR0 TS/EM/MP/NE/AM/CD/NW, CR4 OSFXSR and the other feature bits: nothing (in the code context, or not modelled).

## The hand-off and the way in

- **Out:** a far JMP or far RET that loads a 64-bit CS (L = 1) with LMA = 1 completes in the pass; the loop's next top
  finds `interpreted_mode()` = None and returns to `find_from`'s loop (through `boundary`) or to `start`'s dispatch. RIP
  is the 32-bit offset; the GPRs' upper halves are what 32-bit writes left (zero after INIT). With LMA = 0 the L bit is
  ignored: such a CS is 16- or 32-bit code and stays interpreted.
- **In (`refuse_compatibility`'s CPL-0 arm):** the halt moves into a cold out-of-line `interpret::kernel_compatibility`
  that returns for a CPL-0 target with L = 0 and halts as today otherwise (L = D = 1, or CPL 1-2 from RET). The
  transfer then completes in `far_transfer`/`far_return`, and `commit`'s `ends_in_mode` records the interpreted state
  (as the plan's `commit` arm). The CPL-3 arm is untouched. IRET reaches the same arm: an IRETQ to CPL-0 compatibility
  code is also run (with a null SS where x86 raises #GP(0): a deviation no known code meets).

## Files

`crates/abi`: `interp/system.rs` (new) and its tests, `execution.rs` (the details). `crates/runtime`:
`execution/interpret.rs` (the arms, `mod transitions`), `execution/interpret/transitions.rs` (new), `execution/segments.rs`
(the CPL-0 arm). `crates/host`: `execution/diag.rs` (detail names). Tests: `tests/interp` (`natemu-pending.txt`
emptied; `interp_faults.py` and case `natemu-faults`; new self-tests for HLT and the typed stops; one corpus value,
below), `tests/system` (`initsipi-interpreter` with every scenario but `bsp-init`; the interpreted-HLT scenarios where
the runner can state them), `tests/boot` (`exit-ap-natemu` with the interpreter on; `kut-natemu` if it passes).

**One corpus change:** `R-` and `P32-mov_cr4,_r32-02` write CR4 = 750H, which includes MCE; natemu's profile does not
advertise MCE (`cpu_profile.json`: reserved), so natemu raises #GP(0) where QEMU's and KVM's CPU models load it. The
value becomes 710H and those two cases' answers are seeded again (QEMU and KVM, through the framework).

## Risks for a running 64-bit OS

- `refuse_compatibility` is inlined into `same_mode` (so `far_transfer`) and `far_return`: their disassembly differs in
  that arm alone (a call instead of a copy of `State` and a halt; their frames may shrink). Behaviour changes only for a
  CPL-0 far transfer into L = 0 code, which halted before (Ubuntu 6.8's AP route, the LA57 switch, kvm-unit-tests' 5-level
  tests); CPL-3 transfers keep their #GP deviation (the lock cases).
- Everything else runs only from interpreted state. `find_from`, `sys_fast`, `sys_commit`, `system`, `commit`,
  `set_cpl` are not edited; checked by disassembly against e21b0209, with `transaction` still inlined.
- The resident grows (a few KiB) and the interpreter's gate path deepens: the static stack case.
