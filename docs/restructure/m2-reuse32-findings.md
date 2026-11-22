# M2: reuse of translated 32-bit code

Branch `m2-reuse32` (FEX branch `natemu-m2-reuse32`, 9554e5b). November 21 and 22, 2026.

Since M2 task 2 natemu compiled every request for 32-bit code: it kept no answer, moved none to another address, used
no persistent cache and reused nothing inside the guest ([task 2's findings](m2-task2-findings.md)).
[Checkpoint B](m2-checkpoint-b-findings.md) measured what that costs: the i386 configure of the CPython build spent 69%
of its vCPU time waiting for the compiler, over 14.3 million compiles, of which 0.6% were new bytes, 91% the same bytes
at another address (position-independent programs and libraries load at a new address in every process) and 7% the
same bytes at the same address.

32-bit code now uses everything 64-bit code uses, with no parallel mechanism: the host's kept answers, relocation and
persistent cache, and inside the guest a vCPU's own templates and the shared pool. The one thing it does not use is
position-independent sharing (below).

**Result** (bench case `io5-solo32`, M4, 4 vCPUs, seconds; one run each; "all of it" is this branch at eedae4a2,
November 22):

| Step | Checkpoint B | host side only | all of it | QEMU |
| --- | ---: | ---: | ---: | ---: |
| 64-bit configure | 58.8 | 58.3 | 57.5 | 94.9 |
| 64-bit `make -j4` | 89.6 | 89.1 | 90.0 | 357.6 |
| i386 configure | 374.2 | 149.2 | **106.7** | 110.9 |
| i386 `make -j4` | 223.2 | 126.3 | **114.7** | 464.0 |

The i386 configure is 3.5 times faster than at Checkpoint B and now level with QEMU's; the make is 1.9 times faster and
4.0 times QEMU's speed. Building for i386 costs natemu 1.9 times the 64-bit configure and 1.3 times the make (Checkpoint
B: 6.4 and 2.5; real x86 and QEMU: 1.2 to 1.3).

## What changed

### The key of a kept answer (`crates/host/src/execution.rs`)

The answers table holds a 32-bit answer under its RIP with a tag in the upper half, which a 32-bit address leaves free
(`tag32`): bit 62 (32-bit code), the four null-segment bits (32 to 35) and a 13-bit x87 guess field (from bit 36). Bit
62 set with bit 63 clear is no canonical address, so a tagged RIP never equals a 64-bit one: 64-bit code also runs
below 4 GiB, and never shares an entry with 32-bit code at the same address. The tag is part of the table's key beside
the page offset, so an answer under another tag is in another bucket, never a hit. The gating vector stays at fexc's
15 arguments.

The guess field: fexc reads the x87 guess only when it compiles a unit that contains x87 instructions, and reports the
read. An answer that read it is kept under the request's guess. An answer that did not is kept under a value no guess
has (`GUESS_FREE`), once for every guess; a 32-bit lookup tries that entry first, then the one for its guess.

Everything a 32-bit request sends to fexc is in the key:

| Part of the request | Where it is in the key |
| --- | --- |
| code mode (`--bitness 32`), null FS/GS/DS/ES | the tag |
| x87 guess (`--x87-guess`) | the tag, for an answer that read it |
| block limit (a step is limit 1) | key word |
| page offset of the RIP | key word |
| the guest bytes the compile read | key word (first four) and the entry's prefix |
| SSE/AVX/alignment/x87/WAIT gates, x87 FPCR, unmasked SIMD and x87 exceptions, DAZ, FTZ, accurate mode, push-run | the gating vector, each compared where fexc reports it read |
| host features | one table per feature set |
| stack-check width, segment bases, register map | follow from the mode |

Two guards keep it that way. `Compiler::keyed_read` fails the request when fexc reports reading an argument the table
does not match by (anything above the guess for 32-bit code; the guess itself for 64-bit code, which is sent none).
And fexc now reads the two arguments that decide a unit's x87 run state at entry (`--accurate` without AFP,
`--x87-unmasked`) through its reporting wrappers; before, it read them directly, and they were only safe because
another reported read happened to cover them (`tools/fexc/fexc.cpp`).

Where the tagged RIP is used, and where the plain address:

- tagged (the table only): `key`, `Shard::find`, `Shard::insert`, `Answers::lock`/`find`/`insert`,
  `Entry::fingerprint`;
- plain address: `Entry::rip`, the relocation distance and both relocators, admission, fexc's request and the
  persistent cache's key (`body`, `disk_body`), the reuse check, the pool's publish, the performance log.

### Relocation (`crates/abi/src/execution.rs`, `crates/host/src/relocation.rs`)

In 32-bit code FEX masks every address made from the unit's entry to 32 bits and loads it with the same constant
loader as in 64-bit code, which picks the 32-bit move form for it (one or two words); the host's copy of that loader
already encodes that form. The move rule gained a mode (`relocate_move_mode`, `relocate_code_mode`; the old names are
the 64-bit rule). For a 32-bit object:

- only the 32-bit form is accepted, before and after, re-encoded by the loader's rules in as many words;
- refused, so compiled: a change of word count (a target whose upper or lower 16 bits are zero on one side only), and
  anything that leaves [0, 4 GiB) at either address: a RIP, a block end at or past 4 GiB, a relocated value old or new.
  A target that wraps at 4 GiB is therefore never relocated, whether or not its site masks.

64-bit objects refuse the 32-bit form as before.

### Persistent cache

On for 32-bit code. Its key is the whole request with the RIP reduced to half and page offset, so mode, null segments
and guess were in it already. Tests run with it off; one unit test covers it.

### Reuse inside the guest (`crates/runtime/src/cache.rs`, `execution.rs`)

64-bit code has two further layers, both in the guest-side runtime and both ending in the same copy routine:

- **a vCPU's own templates**: a block whose answer depended only on its own pages' bytes is marked a template; when
  the same physical page turns up at another address (a library mapped by the next process), the runtime copies the
  block and rewrites its addresses, without asking the host;
- **the shared pool**: the host publishes every template into a read-only ring all vCPUs map, found by content, for
  the vCPU that has no template of its own.

The host kept 32-bit code out of both with one condition. That condition is gone, and three things were made right
for 32-bit code:

- **The key.** Templates and pool entries are found by the code context, the page offset and the bytes. The context
  already holds the code mode and the four null-segment bits. It holds no x87 guess, so **a 32-bit unit whose compile
  read the guess is not a template**: it is neither copied nor published, and each process asks the host for it, which
  serves it from its kept answers. Every other 32-bit unit is a template exactly as a 64-bit one is. This is exact: a
  unit that did not read the guess is the same code under every guess, by fexc's own report. (Whether x87 units should
  become templates too, keyed without the guess, is an open question for the owner; it was not built.)
- **The copy routine.** Both copies (from an own template, from the pool) rewrite the block with the mode-aware rule,
  the mode taken from the block's code context. A refusal sends the request to the host, as any refusal does.
- **The one word the runtime changes.** A 32-bit block that begins with the test for an open x87 run has, behind its
  body, the run's end and a branch back to its third word. When the runtime splits a block into hot and cold parts it
  points that branch at the hot copy. A copy of such a block now gets the word back as admitted before it is rewritten
  and split afresh (as it already did for link points), and the reuse check compares that word as the one it stands
  for. Before, such a block was refused as a source of copies, and the check would have stopped on a correct copy made
  from the pool.

**Position-independent sharing stays 64-bit only.** For 64-bit user code the runtime can rewrite a template once into
a form that reads its page's address at run time, and share that code between addresses instead of copying it. The
rewrite needs address moves of two or three words and adds in 64 bits; 32-bit moves are one or two words and wrap at
4 GiB, and nothing in a shared instance checks that. The rule that decides sharing refuses 32-bit contexts
(`natemu_abi::execution::cache::shares`); they are copied.

### FEX (two commits, pin 9554e5b)

- **A FISTP m64 that starts its unit** compares its own address with the address the runtime kept beside an integer
  (the rerun of a store that faulted). The address was a plain constant, so an answer moved to another address still
  compared with the address it was compiled at: the kept integer was never used there. The `libm32` campaign under the
  reuse check found it. It is a guest-RIP relocation now, and admission accepts the block's own RIP as such a move's
  value in the first instruction of 32-bit code. Only such units' code changes.
- **An exit whose target is a plain constant**, not made from the unit's entry, was recorded as a guest-RIP relocation,
  so relocation would have shifted an absolute address. No lowering produces one today (probed: `mov eax, imm; jmp eax`,
  `push imm; ret`, `jmp [abs]`, far transfers); it now leaves as a computed exit without a relocation record.

64-bit output is unchanged (`fexc-replay`).

## Proof

- **Unit tests with the real compiler** (`answers_for_32_bit_code_are_kept_by_mode_segments_and_guess`,
  `rare_move_forms_of_32_bit_code_move_as_fexc_compiles_them`, the persistent-cache test,
  `translation_publishes_templates_only_and_notes_gating`): every moved answer is compared with fexc's own compile at
  the new address, for the common two-word form and the rare ones (`movn`, bitmask, one `movz`, a target of 0, a block
  ending at 4 GiB, wraps both ways, a unit starting with FISTP m64); 64-bit and 32-bit code at the same low address
  never share an entry; a 32-bit template is published once under every guess and a unit that read the guess never.
- **Unit tests without it**: the move rule and each refusal (`the_move_rule_of_32_bit_code`), the tag
  (`answers_of_another_tag_are_never_found`), and the runtime's shared rules
  (`templates_of_32_bit_code_are_copied_never_shared`: 32-bit contexts are never shared; a block on the last page
  below 4 GiB that would end at 4 GiB is refused by the rule both copy routines call; the run end's return word).
- **The reuse check** (`NATEMU_CHECK_REUSE=1`): every answer the host uses again is compiled again at that address
  and compared, now also a 32-bit answer found at its own address (which checks the key, not only relocation); and
  every block the runtime copies, from a template or the pool, is requested from the host and compared in full. A
  mismatch stops the run. The `torture32` and `libm32` campaigns now always boot under it (`check_reuse = true`,
  `tests/correctness/family.toml`).

  Runs under the check, all on the M3 at eedae4a2 (the branch's code; this commit changes only comments, a test's
  position and a variable's name), November 22:

  | Run | Result | Wall time (cap 3600 s) |
  | --- | --- | ---: |
  | unit tests (`tests/unit`) | 630 passed, 13 skipped | 169 s |
  | `tests/correctness -k i386`, `NATEMU_CHECK_REUSE=1` | 132 passed, 12 expected-fail | 363 s |
  | `torture32` and `libm32` (no AFP, fast and accurate), three units, run one after another | 5,317 passed, 982 expected-fail | 1,759 s for the three; the longest, `torture32`, 756 s; the two `libm32` runs 481 s and 521 s |
  | `tests/static -k fexc` | 276 passed | 1,137 s |
  | `tests/correctness -k i386`, without the check | 132 passed, 12 expected-fail | 260 s |

  The expected failures are the ones the suites already carry in their expectation files; none is new. Each of the three campaign units stayed under a
  quarter of its 3600 s cap. The unit run's one note is a known bug that now looks fixed
  (`block-abandoned-worker-retire-timeout`), unrelated to this branch.

The runtime has no unit tests of its own, so the copy of a split block with the run test, and the refusal on the last
page as the runtime calls it, are covered by the campaigns under the check and by the unit tests of the rules they
call, not by a test that builds that exact block.

## Results

Measured November 22, 2026 on the M4, alone on the machine, one run each. "Checkpoint B" is
[its run](m2-checkpoint-b-findings.md) of the same cases; "host side only" is this branch before reuse inside the guest
(kept answers, relocation). The timed runs are in the table at the top.

Profile (bench case `io5-solo32-profile`; `tests/io/io5_compiles.py`), shares of vCPU time that is not halted. The
last profile was taken one commit before the FISTP m64 fix, which changes only units that start with that
instruction; the timed run is of the final code.

| Step | waiting for the host: B | host side only | all of it | in translated code | copying (templates, pool) | linking and patching |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| i386 configure | 69% | 34% | **4%** | 25% | 19% | 15% |
| i386 make | 49% | 12% | **3%** | 67% | 5% | 7% |

Busy vCPU time in the i386 configure, in seconds (the profile's buckets; 112.4 s in all, summed over the 4 vCPUs),
beside the 64-bit configure (60.4 s in all):

| Bucket | i386 configure | 64-bit configure |
| --- | ---: | ---: |
| in translated code | 27.8 | 23.2 |
| copying (templates, pool: "reuse") | 14.9 | 1.4 |
| delivery | 14.2 | 3.0 |
| linking | 12.6 | 4.1 |
| pool lookups | 6.9 | 2.2 |
| patching | 4.4 | 0.8 |
| requests to the host | 991,778 | 1,077,758 |

Requests to the host and compiles in the i386 configure:

| | Checkpoint B (first 62%) | host side only (first 62%) | all of it (whole step) |
| --- | ---: | ---: | ---: |
| translation requests | 14.3 million | 15,307,090 | **991,778** |
| compiles | 14,344,170 (182.4 s) | 98,733 (1.8 s) | 125,391 |
| of bytes not compiled before in the boot | 91,365 | 91,015 | 117,576 |

The host side removed the compiles; reuse inside the guest removed the requests. The i386 configure now asks the host
about as often as the 64-bit configure does (1.08 million). What it spends beyond the 64-bit configure is copying
(19% against 6%): every new process copies the blocks of its shell and libraries again, where a 64-bit process shares
position-independent code. That is the remaining gap between the two (106.7 s against 57.5 s), and a gap, not a flaw:
closing it means sharing for 32-bit code, which needs fexc to emit fixed-width address moves there.

Over the whole boot the host was asked 43.3 million times before reuse inside the guest and 3.0 million times with it;
the runtime made 17.1 million copies before and 47.9 million with it; pool hits went from 5.9 to 15.1 million.

**64-bit code is not slower.** The 64-bit steps of the same runs, before and after reuse inside the guest:

| | configure, before | after | make, before | after |
| --- | ---: | ---: | ---: | ---: |
| seconds (timed run) | 58.3 | 57.5 | 89.1 | 90.0 |
| requests to the host (profile) | 1,057,692 | 1,077,758 | 756,855 | 754,906 |
| seconds copying own templates (profile) | 1.5 | 1.5 | 1.0 | 1.0 |
| seconds in pool lookups (profile) | 2.2 | 2.2 | 4.2 | 3.9 |

The log keeps the pool's counters (probes, hits, refusals by reason) only as totals of the boot, not per step, so the
64-bit steps are compared by what it has per step: requests to the host (a pool miss ends in one) and the time in
the two copy paths. The totals, for the record: entries published 979,371 (572 MB) before and 1,649,630 (1,007 MB)
after, into a ring of 256 MB; lookups refused because the entry had been overwritten 1.92 million before and 2.55
million after.

A limit of this evidence: in `io5-solo32` the 64-bit steps run before the i386 ones, so they never see a ring that
32-bit entries have filled. The ring now turns over about four times in the boot instead of twice. A workload that
mixes 64-bit and 32-bit processes could lose 64-bit entries to that sooner; this run does not measure it.

**The guess in the key.** Before answers without a guess were kept once, at most 5,771 of the 98,733 compiles in the
i386 configure were of bytes compiled before, for all causes together (5.8%, 0.1 s); the 64-bit configure, which has
no guess, compiles bytes again at the same rate (5.7%). So the guess never cost much here; keeping such answers once
is for the pool, which would otherwise have published the same template once per guess.

**`startup32`** (bench case `guest-startup32`: one static program started 40 times on one vCPU; M4, this branch at
eedae4a2, November 22, one run): 7.16 s in its second pass (7.59 s in the first), against 7.18 s at Checkpoint B. No change, as expected: the program loads at the same
address every time, and a vCPU's blocks are found again by address and physical page without any request.

## The alternative: one block for every address

QEMU keys its translated blocks by guest physical page and CPU flags, and with position-independent blocks
(`CF_PCREL`) one block serves every virtual address without patching: the block reads the current PC from CPU state
where it needs it. natemu keeps one answer per (bytes, page offset, mode) and makes a patched copy per address.

natemu has part of QEMU's approach already, for 64-bit user code: the position-independent sharing described above. It
is not the keying QEMU uses (blocks are still found by address and by physical page), and it does not cover 32-bit
code.

The numbers say what copies cost 32-bit code now that the host is out of the path: 19% of the i386 configure, the
largest part after translated code itself, and the reason it takes 1.9 times the 64-bit configure. Extending sharing
to 32-bit code is the next step if that matters: fexc would emit address moves of a fixed width in 32-bit code (a
change of its 32-bit output), and the shared form would need the 4 GiB wrap.

## What is left

- **32-bit code is excluded from position-independent sharing**, because fexc builds 32-bit addresses in one or two
  words and the shared form needs a fixed width and the 4 GiB wrap. It costs copying, linking and patching: in the i386
  configure 31.9 s of vCPU time (14.9 + 12.6 + 4.4) against 6.3 s for the 64-bit configure (1.4 + 4.1 + 0.8). The planned fix is three-word 32-bit address moves with a 32-bit add.
- **"Delivery" is likely the INT 0x80 system calls of the 32-bit programs, but this is unconfirmed.** The bucket counts
  all time spent delivering guest events (exceptions, interrupts and software interrupts through the IDT) and does not
  split them by vector, so the profile cannot show it. 14.2 s against 3.0 s in the 64-bit configure fits a
  system-call-heavy 32-bit shell, nothing more. A per-vector count would settle it.
- **x87 units are not pooled**: a unit whose compile read the x87 guess is copied by no vCPU and published to no pool;
  each process asks the host, which serves it from its kept answers. Whether to key such units without the guess is open.
- **A 32-bit request at a RIP at or above 4 GiB is now a host error, so the run stops.** A guest IRET or far return to
  compatibility mode with a high RIP should raise #GP first. The segment and far-transfer task (task 5) should check this.
