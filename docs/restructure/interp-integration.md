# The boot-time interpreter in the resident

Status: November 22, 2026, built (M2 Part 2, after B1 (b); plan sections 3-5 are the spec). Scope: everything
`natemu_abi::interp::exec::step` executes, plus CPUID, RDMSR, WRMSR, WBINVD, LGDT, LIDT and MOV from CR0/CR2/CR3/CR4
(CLI and CLD are the core's). Typed stops for now, the next step's: protected-mode far JMP/RET and MOV Sreg, MOV to
CR, HLT, the hand-off to 64-bit code. INIT and SIPI are the parallel CPU start-up work's. Code:
`crates/runtime/src/execution/interpret.rs`.

## The loop (`interpret::run`)

One instruction per pass, until `Cpu::interpreted_mode()` is `None`; it then returns to `find_from`'s loop (through
`boundary`), or `start` dispatches.

1. `rip &= 0xffff_ffff`; `io_checkpoint`, `take_invalidations`, `lapic::poll`: `find`'s order.
2. Both shadows cleared (the interpreter sets and honours none). A pending trap (`State::debug & TRAP` but
   `INTERPRET`), RFLAGS.TF or any DR7 enable: typed stop `interp_debug`.
3. Events: `interpret::events` is the one place they are read. It calls `events::undelivered` (`find`'s `next`, nothing
   taken) and turns any event into the typed stop `interp_event` (`event_stop`: kind and vector), left pending. The
   CPU start-up work's `events::interpreted` (which takes INIT itself) replaces that one call.
4. Mode: protected mode at CPL > 0, paging without EFER.LMA, or paging controls `Cpu::paging` refuses: `interp_mode`.
5. The record at linear = CS.base + EIP (32-bit wrap): a cached one whose bytes still match, else a decode; then
   `exec::step` on a copy of the bank (the memory layer borrows the record), written back on `Done` (RF cleared).

Resident operations: CPUID, RDMSR, WRMSR, WBINVD through the existing `transaction` (`#[inline(always)]` now: out of
line it put a call on the fast system-call path, `system`; with it `system` is unchanged), its `Err` a fault stop with
nothing committed, its `commit`'s `debug::INTERPRET` cleared; LGDT/LIDT set GDTR/IDTR from the core's operands; MOV from
CR writes the register (other CR numbers #UD). The rest: `interp_unsupported` with the operation's detail.

## Fetch

Each byte at EIP + k inside the CS limit (#GP(0) past it; no execute-right check), through the current translation
(paging off: the identity; compatibility mode: a fetch walk with `cpu.paging()`, CPL 0; `fetch_walk` keeps its halt),
from guest RAM only (else `interp_memory`, detail fetch). A page's walk is made only when a byte of it is wanted. A
decode fetches to the end of RIP's page (at most 240 bytes), stopping short at the first byte it cannot fetch and keeping
that byte's stop; only when the first instruction is truncated does it fetch the next page, up to 15 bytes in all, and
ask again; a fault there, or past the CS limit, is raised only for an instruction that reaches it. The request goes out
even with no byte fetched, so the host's stop answer comes before a fetch fault there. Validation fetches the record's
`length` bytes the same way.

## Records

Filed with the cache's `file` as `SYSTEM | INTERPRETED`, the `Insn` (56 bytes) as the tail
(`cache::place_interpreted`), keyed by the linear address and `interpreted_key(cpu)` = `interpreted_context(mode, cpl)` |
CS.D << 5 (the decode depends on CS.D, which real mode's classification does not hold), no source page (no page chain,
no code-write mark). Found by `cache::interpreted_at`, its own L2 probe that ignores currency (the translated blocks'
`lookup` and `cached_at` are not called, so their code is unchanged), and validated by comparing `bytes[..length]` with a
fresh fetch at every execution: as fetching every time, through the current translation and the CS limit. This replaces
section 5's "through `lookup` and `revalidated`" (`revalidated_unpaged` is gone). A mismatch or a failed fetch decodes
again; the fresh decode's first record executes from the reply unvalidated (no livelock), and each record of the run
replaces the tail of the record under its key or is filed. Stop records (UD, TOO_LONG, OTHER) are not cached. A full
arena: `flush` and file again, as `find` does. Each reply record is checked (well formed, its bytes those sent at its
position and zero after them, lengths within `available`), else `bad_reply`.

Exclusion from translated code's paths: the key has `INTERPRETED_CONTEXT` (bit 63), which no `code_context()` has, so
`kept_system`, `sys_fast`, `miss_fast` and `find_from` never find one; `cut`/`cut_one` find no INBOUND, out slot or L1
entry; `link`, `template`, `first_test_fires` and the host's block passes exclude SYSTEM. Disassembly against the parent
commit (release residents, literal and anonymous-symbol offsets aside): `find_from`, both `lookup` instances, `mapped`
(with `revalidated` inlined), `revalidated_from_record`, `sys_fast`, `miss_fast`, `system`, `commit`, `set_cpl`,
`translate`, `fetch_walk`, `segments::pages`, `enter`, `linker`, `step_here`, `cut_one`, `invalidate`, `current_at`,
`kept_system` and `trap_due` identical; `cache::file` (placement) swaps two registers; `split_lock` inlines its
`split_begin`, `mmio::ones` calls `mmio::page` out of line. Resident 240,164 -> 260,784 bytes.

Stops (`Hook::at`): the host's `REQUEST_DECODE` handler asks `stop_at` and `hook.at` at CS.base + EIP first (a stop parks
the vCPU, `REPLY_PARK`; a test hook's `At::System` there is an error) and ends the run before `hook.boundary` or
`stop_at` (`interp::run_to`), so no record is made for a stop's address.

## Memory layer (`interpret::Mem`, layers 2-5)

`check(linear, size, write)`: every page touched is translated first (the first walked at `linear`, so CR2 is exact;
paging off the identity, wrapping at 4 GiB; compatibility mode the walk, A/D by CAS, #PF with the walker's error code),
then classified: RAM (`mmio::ram`, with the memory type's barrier as `segments::pages`); a device's page
(`mmio::unassigned`: the LAPIC page, a remembered claim or the host's `REQUEST_PAGE` answer): `interp_memory` with the
page; unassigned (the legacy hole among them): reads all-ones, writes ignored; then `split_wait`. Up to four pages per
instruction are kept for `read`/`write`/`compare_exchange` (a miss is an interpreter bug: a halt). 1, 2 or 4 bytes inside
16 are one Arm access (inline `ldr`/`str`, aligned or not: FEAT_LSE2), byte by byte across 16 (the existing tearing
deviation); stores follow `segments::copy`'s code-write protocol (`code_write` when translated, `dsb ish`,
`rechecked`). LOCK: `casal{b,h}` inside 16 bytes, else (or on unassigned bytes) read-compare-write under
`cache::split_begin`/`split_end`, as `split_lock`.

## Typed stops

`interp_fault` (80), `interp_unsupported` (81), `interp_memory` (82), `interp_event` (83), `interp_debug` (84),
`interp_mode` (85), the record in `exec::stop::INTERP_FAULT`'s documentation: `State::native[0]` = mode | op << 8 | bytes
<< 16 | detail << 24, `native[1..3]` the bytes; `State::fault` = [vector, CR2, error code, RIP] for a fault, [0, linear,
page, RIP] for memory, [vector, ...] for an event or the DR6/DR7 bits. Nothing is committed (RIP at the instruction). The
program driver reports `fault` for these stops; the host's report (`diag::report`) adds a line naming the mode, bytes,
operation and record.

`debug::INTERPRET` (DR6 bit 12) stays out of the guest: DR6 is written from `State::debug` only in `debug_exception`,
reached through `trap_due` after its INTERPRET test returns and through `code_break` with no bits; MOV to DR6 masks the
reserved bit 12; the interpreter delivers no #DB and runs no MOV DR (typed stops). `debug_exception` also masks the bit.

## Stack

The exception stack is 80 KiB (0x5c000-0x70000, its own commit). Static case `stack` (M2 Max, run
20261122-053137-98b3): the deepest gate path now runs through the interpreter with a transaction under it (`run`'s frame
2,960 bytes, the transaction inlined into it), 20,960 bytes, 25,296 with the nested entry, of 61,440 allowed (36,144
headroom beyond the 25% reserve); boot 11,072 of 24,576.

## Cases

Case `natemu` (M2 Max, run 20261122-052809-a4eb): 1,574 of the 1,716 corpus cases match their answers; the other 142
stop at a resident operation not built yet (far JMP 25, far RET 11, segment loads 38, MOV to CR 68), listed in
`tests/interp/natemu-pending.txt`. Case `natemu-machine` adds 12 interpreted-mode self-tests: an unaligned LOCK XADD and
LOCK BTS inside 16 bytes (FEAT_LSE2 at EL1: no alignment fault) and a LOCK XADD across 16 bytes; an instruction ending
at a page end before an unmapped page, one crossing into it (#PF, CR2 the page, error 0); an instruction ending at the CS
limit, the byte after it and one crossing it (#GP(0)); unassigned and device pages; self-modifying code; three typed
stops.

## Risks

- Unassigned pages cost a host exit per access (`REQUEST_PAGE`, no cache); MTRRs off (power-on) make every access UC,
  a barrier each. Boot code rarely touches either.
- A decode exit per new instruction (runs of up to 16 cut that); a stub page shared with translated code pays the
  code-write protocol (plan section 5).
- The watchdog: a long interpreted loop makes no host exit, as translated code; a spin wait longer than the driver's
  watchdog is a forced stop.
- Hook stops must be static (the program driver's are): a record cached before a hook appears at its address would
  pass it.
