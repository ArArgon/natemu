# The boot-time interpreter in the resident: integration design

Status: November 22, 2026, design (M2 Part 2, after B1 (b); plan sections 3-5 are the spec). Scope: everything
`natemu_abi::interp::exec::step` executes plus CPUID, RDMSR/WRMSR, LGDT/LIDT, WBINVD (CLI, CLD are the core's) and MOV
from CR0/CR2/CR3/CR4 (trivial). Typed stops for now, the next task's: protected-mode far JMP/RET and MOV Sreg, MOV to CR,
HLT, the hand-off to 64-bit code. INIT and SIPI are the parallel task's.

## The loop (`interpret::run`)

One instruction per pass, until `Cpu::interpreted_mode()` is `None` (then it returns to `find_from`'s loop, which
`boundary` already continues; `start` calls `run`, then `dispatch`):

1. `rip &= 0xffff_ffff`; `io_checkpoint`, `take_invalidations`, `lapic::poll` (find's order).
2. Debug: a pending trap (`State::debug & TRAP` but `INTERPRET`), TF, or any DR7 enable: typed stop `interp_debug`.
3. Events: `interpret::events`, the one place (the INIT/SIPI task extends it): `events::interpreted(s)` is `next()`
   without the shadow test and without taking anything; any event (NMI, maskable interrupt with IF = 1, ExtINT) is a
   typed stop `interp_event` naming kind and vector, left pending.
4. Mode support: protected mode at CPL ≠ 0, or CR0.PG without EFER.LMA (legacy/PAE paging), or `Cpu::paging()` `None`
   with PG set: typed stop `interp_mode`.
5. The record at linear = CS.base + EIP (wrapping at 32 bits): a cached one whose bytes still match (below), else a
   decode; then `exec::step` on a copy of `s.cpu` (the memory layer borrows `s`), written back on `Done`; a resident
   operation (below); an `Exit` is a typed stop.

Resident operations: CPUID, RDMSR, WRMSR, WBINVD through the existing `transaction` with a `System { kind, length }`
(no delivery: `Err` is a fault stop; its `commit` sets `debug::INTERPRET` through `ends_in_mode`, which the loop clears,
and advances RIP with the 32-bit wrap `exec::complete` has); LGDT/LIDT set GDTR/IDTR from the core's operands;
MOV from CR writes the register (CR other than 0, 2, 3, 4: #UD). The rest: `interp_unsupported`.

## Fetch

Each byte at EIP + k must lie within the CS limit (#GP(0) past it); no execute-right check. Paging off: the identity
(`fetch_unpaged`, guest RAM only). Compatibility mode: a fetch walk with `s.cpu.paging()` (CPL 0; `fetch_walk` keeps
its `controls` halt). A decode fetches to the end of the page, into the next page only when fewer than 15 bytes remain
(at most 15 in all then), at most 240, straight into the staging `Decode`; it stops at the first byte it cannot fetch
and remembers why (#GP(0), #PF with its error code and CR2, or not RAM: a typed stop `interp_memory`). Validation
fetches the record's `length` bytes the same way. The request goes out even with 0 bytes, so the host's stop answer
comes before a fetch fault there.

## Records

Placed with the cache's `file` as `SYSTEM | INTERPRETED`, the `Insn` (56 bytes) as the tail, keyed by the linear
address and `interpreted_key(cpu)` = `interpreted_context(mode, cpl)` | CS.D << 5 (real mode's decode depends on CS.D,
which the mode does not hold), `source_gpa` unused (on no page chain: no code-write mark). Found with a new
`cache::interpreted_at(linear, key)`, an L2 probe that ignores currency, and validated by comparing `bytes[..length]`
with a fresh fetch at every execution: equivalent to fetching every time, through the current translation, with the CS
limit (so `revalidated_unpaged` goes: deviation from section 5's "through `lookup` and `revalidated`"). A mismatch, or a
fetch failure, decodes again; a fresh decode's first record executes from the reply without validation (no livelock),
and each record of the run replaces the tail of the record at its key in place, or is filed. Stop records (UD, TOO_LONG,
OTHER) are not cached. Full arena: `flush` and file again, as `find` does. The reply is checked as the contract says
(well formed, its bytes those sent at its position, lengths within `available`), else `bad_reply`.

Exclusion: the key has `INTERPRETED_CONTEXT` (bit 63), which no `code_context()` has, so `kept_system`, `sys_fast`'s
lookup, `miss_fast`'s and `find_from`'s never find one; `cut`/`cut_one` see no INBOUND, out slots or L1 entry; `link`,
`template`, `first_test_fires` and the host's block passes exclude SYSTEM. No hot path changes.

Stops (`Hook::at`): the host's `REQUEST_DECODE` handler asks `stop_at` and `hook.at` at the request's linear address
first (a stop parks the vCPU, `REPLY_PARK`, as a translation does; a test hook's `At::System` there is an error) and
ends the run before `hook.boundary(linear)` or `stop_at`, so no record is ever placed at a stop.

## Memory layer (`interpret::Mem`, layers 2-5)

`check(linear, size, write)`: each page touched (the first walked at `linear`, so CR2 is exact): paging off
`unpaged_pages`, compatibility mode a walk like `segments::pages` with `s.cpu.paging()` (#PF with the walker's error
code, A/D set by CAS, the memory-type barrier); then the class: RAM (`mmio::ram`); a device (the LAPIC page, a claimed
page, or the host's `REQUEST_PAGE` answer `claimed`): `Exit::Stop`, the guest-physical page kept for the report;
unassigned (the legacy hole among them): reads all-ones, writes ignored; `split_wait`. Up to four pages are kept per
instruction for `read`/`write`/`compare_exchange`, which find their page there (a miss is an interpreter bug: a halt).
Nothing commits before every check of the instruction (the core's order). Accesses: 1 byte; 2 or 4 bytes inside 16 bytes
as one Arm access (aligned or not: `ldrh/ldr/strh/str` by inline asm; FEAT_LSE2); crossing 16 bytes byte by byte (the
existing tearing deviation); across pages in two parts. Stores: `code_write` first when `cache::translated(gpa)`, then
`dsb ish`, then `cache::rechecked` (as `copy`). LOCK: `casal{b,h}` on the alias inside 16 bytes; crossing 16 bytes under
`cache::split_begin`/`split_end` (read, compare, write), as `split_lock`.

## Typed stops

New codes, numbered apart: `interp_fault` (80), `interp_unsupported` (81), `interp_memory` (82), `interp_event` (83),
`interp_debug` (84), `interp_mode` (85). The record, nothing committed (RIP at the instruction): `State::native[0]` =
the interpreted mode | op << 8 | bytes << 16 | detail << 24 (event kind, memory kind), `native[1..3]` the instruction's
bytes (the record's, or those fetched); `State::fault` = [vector, CR2, error code, RIP] for a fault, [vector, linear,
guest-physical page, RIP] for memory, vector and kind for an event. The host's report (`diag::report`) adds a line with
these: mode, bytes, op, the exception or what stopped it.

`debug::INTERPRET` (DR6 bit 12) stays out of the guest: DR6 is written from `State::debug` only in `debug_exception`
(`trap_due`'s path, after its INTERPRET test returns, and `code_break` with no bits), MOV DR6 masks bit 12 (reserved),
and the interpreter delivers no #DB and runs no MOV DR (typed stops). Belt and braces: `debug_exception` masks it.

## Stack

The interpreter's body starts at 12,864 bytes under the deepest gate path. `EXCEPTION_STACK_TOP` moves to 0x70000 and
its reservation grows from 32 KiB to 80 KiB (0x5C000-0x70000, the free 0x64000-0x70000 taken; the host's span and the
static check read both from the sources): its bound with the nested entry may then be 61,440 bytes, against about
28,000 expected. Its own commit, with the replay and stack checks; the numbers come from case `stack`.

## Files

abi: `lib.rs` (stack top), `execution.rs` (stop codes, names, `interpreted_key`, the stop record), `interp.rs` (doc).
resident: `execution/interpret.rs` (loop, fetch, decode request, records, `Mem`, resident operations, stops),
`cache.rs` (`place_interpreted`, `interpreted_at`, `interpreted_of`, in-place replacement), `execution/events.rs`
(`interpreted`), `execution/mmio.rs` (a non-RAM page's class), `execution.rs` (`debug_exception`'s mask). host:
`execution.rs` (stack span; the decode handler's stop and boundary), `interp.rs` (the run's byte limit),
`execution/diag.rs` (the stop's report). tests: `tests/interp/family.toml` and `run_interp_natemu.py` (the natemu cases,
with the list of those that stop for now; typed-stop self-tests), `docs/restructure/interp-cases.md`.

## Risks

- Stack: the interpreter calls `transaction` (6,880 bytes deep) under the deepest gate path; the bigger reservation
  is the remedy; checked by case `stack`.
- LSE2: an unaligned `casal` inside 16 bytes not faulting is inferred from translated code's contract (`split_lock`
  handles only accesses crossing 16 bytes); if wrong, the resident halts loudly (an EL1 alignment fault).
- Unassigned pages cost a host exit per access (`REQUEST_PAGE`, no cache); MTRRs off (power-on) make every access UC,
  a barrier each. Boot code rarely touches either.
- A decode exit per new instruction (runs of up to 16 cut that); a stub page shared with translated code pays the
  code-write protocol (plan section 5).
- The watchdog: a long interpreted loop makes no host exit, as translated code; a spin wait longer than the driver's
  watchdog is a forced stop.
- Hook stops must be static (the program driver's are): a record cached before a hook appears at its address would
  pass it.
- The INIT/SIPI task edits `events.rs` beside `interpreted` and `interpret::events`: a merge, not a conflict of design.
