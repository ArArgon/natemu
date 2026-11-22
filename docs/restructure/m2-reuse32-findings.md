# M2: reuse of translated 32-bit code

Branch `m2-reuse32` (FEX branch `natemu-m2-reuse32`, b89eb4c). Since M2 task 2 the host compiled every request for
32-bit code: it kept no answer, moved none to another address and used no persistent cache
([task 2's findings](m2-task2-findings.md)). Checkpoint B measured what that costs: the i386 configure of the CPython
build spends 69% of its vCPU time waiting for the compiler, over 14.3 million compiles, of which 0.6% were new bytes,
91% the same bytes at another address (position-independent programs and libraries load at a new address in every
process) and 7% the same bytes at the same address.

32-bit code now uses the 64-bit code's kept answers, relocation and persistent cache. Nothing parallel was built.

## What changed

**The key (`crates/host/src/execution.rs`).** The answers table holds a 32-bit answer under its RIP with a tag in the
upper half, which a 32-bit address leaves free (`tag32`): bit 62 (32-bit code), the four null-segment bits (32 to 35)
and the x87 guess (13 bits from bit 36). Bit 62 set with bit 63 clear is no canonical address, so a tagged RIP never
equals a 64-bit one: 64-bit code also runs below 4 GiB, and never shares an entry with 32-bit code at the same
address. The tag is part of the table's key beside the page offset; an answer under another tag is in another bucket,
never a hit. The gating vector stays at fexc's 15 arguments, so the template pool's per-context gating note never
sees the guess.

Everything a 32-bit request sends to fexc is in the key:

| Part of the request | Where it is in the key |
| --- | --- |
| code mode (`--bitness 32`), null FS/GS/DS/ES | the tag |
| x87 guess (`--x87-guess`) | the tag, whether the compile read it or not |
| block limit (a step is limit 1) | key word |
| page offset of the RIP | key word |
| the guest bytes the compile read | key word (first four) and the entry's prefix |
| SSE/AVX/alignment/x87/WAIT gates, x87 FPCR, unmasked SIMD and x87 exceptions, DAZ, FTZ, accurate mode, push-run | the gating vector, each compared where fexc reports it read |
| host features | one table per feature set |
| stack-check width, segment bases, register map | follow from the mode |

Two guards keep it that way. `Compiler::keyed_read` fails the request when fexc reports reading an argument the
table does not match by (anything above the guess for 32-bit code; the guess itself for 64-bit code, which is sent
none). And fexc now reads the two arguments that decide a unit's x87 run state at entry (`--accurate` without AFP,
`--x87-unmasked`) through its reporting wrappers; before, it read them directly, and they were only safe because
another reported read happened to cover them (`tools/fexc/fexc.cpp`).

Where the tagged RIP is used, and where the plain address:

- tagged (the table only): `key`, `Shard::find`, `Shard::insert`, `Answers::lock`/`find`/`insert`,
  `Entry::fingerprint`;
- plain address: `Entry::rip`, the relocation distance and both relocators, admission, fexc's request and the
  persistent cache's key (`body`, `disk_body`), the reuse check, the pool's publish, the performance log.

**Relocation (`crates/abi/src/execution.rs`, `crates/host/src/relocation.rs`).** No change to fexc's output was
needed. In 32-bit code FEX masks every address made from the unit's entry to 32 bits and loads it with the same
constant loader as in 64-bit code, which picks the 32-bit move form for it (one or two words); the host's copy of that
loader already encodes that form. The move rule gained a mode (`relocate_move_mode`, `relocate_code_mode`; the old
names are the 64-bit rule, which the resident keeps using unchanged). For a 32-bit object, taken from the object's own
header, which admission holds to the request's mode:

- only the 32-bit form is accepted, before and after, re-encoded by the loader's rules in as many words;
- refused, so compiled: a change of word count (a target whose upper or lower 16 bits are zero on one side only), and
  anything that leaves [0, 4 GiB) at either address: a RIP, a block end at or past 4 GiB, a relocated value old or new.
  A target that wraps at 4 GiB is therefore never relocated, whether or not its site masks.

64-bit objects refuse the 32-bit form as before.

**Persistent cache.** On for 32-bit code. Its key is the whole request with the RIP reduced to half and page offset,
so mode, null segments and guess were in it already. Tests run with it off; one unit test covers it.

**FEX (b89eb4c).** An exit whose target is a plain constant, not made from the unit's entry, was recorded as a
guest-RIP relocation, so relocation would have shifted an absolute address. No lowering produces one today (probed:
`mov eax, imm; jmp eax`, `push imm; ret`, `jmp [abs]`, far transfers); it now leaves as a computed exit without a
relocation record.

## Not done

**The template pool** (blocks shared between vCPUs inside the guest-side runtime) stays 64-bit only. It needs real
design: the pool is keyed by code context without the gating values and requires one gating vector per context, while
32-bit requests of one context differ in the x87 guess; and the runtime's own copy routine would need the 32-bit move
rule and its 4 GiB checks, which is a change to code every block passes through.

## Proof

- Unit tests with the real compiler (`answers_for_32_bit_code_are_kept_by_mode_segments_and_guess`,
  `rare_move_forms_of_32_bit_code_move_as_fexc_compiles_them`, the persistent-cache test): every moved answer is
  compared with fexc's own compile at the new address, for the common two-word form and the rare ones (`movn`,
  bitmask, one `movz`, a target of 0, a block ending at 4 GiB, wraps both ways).
- Unit tests without it: the move rule and each refusal (`the_move_rule_of_32_bit_code`), the tag
  (`answers_of_another_tag_are_never_found`).
- The reuse check (`NATEMU_CHECK_REUSE=1`) now also compiles again a 32-bit answer found at its own address, which
  checks the key and not only relocation. Corpus runs under it: see Results.

## Results

RESULTS

## The alternative: one block for every address

QEMU keys its translated blocks by guest physical page and CPU flags, and with position-independent blocks
(`CF_PCREL`) one block serves every virtual address without patching: the block reads the current PC from CPU state
where it needs it. natemu instead keeps one answer per (bytes, page offset, mode) and makes a patched copy per address.
What a copy costs here: a table lookup, rewriting a handful of move instructions, and placing the block again in the
guest-side cache (admission is skipped once an answer's form is kept). What position-independent blocks would cost:
every direct exit and return address computed at run time instead of loaded as a constant, block links that can no
longer be direct branches to a fixed target, and a second design for the cache inside the guest-side runtime. The
measured numbers above say how much of the wait is left after this change; if copies still dominate, the cheaper next
step is the pool for 32-bit code (one copy per vCPU made inside the runtime without asking the host), not a move to
position-independent blocks.
