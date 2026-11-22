# M2: reuse of translated 32-bit code

Branch `m2-reuse32` (FEX branch `natemu-m2-reuse32`, b89eb4c). Since M2 task 2 the host compiled every request for
32-bit code: it kept no answer, moved none to another address and used no persistent cache
([task 2's findings](m2-task2-findings.md)). Checkpoint B measured what that costs: the i386 configure of the CPython
build spends 69% of its vCPU time waiting for the compiler, over 14.3 million compiles, of which 0.6% were new bytes,
91% the same bytes at another address (position-independent programs and libraries load at a new address in every
process) and 7% the same bytes at the same address.

32-bit code now uses the 64-bit code's kept answers, relocation and persistent cache on the host. Nothing parallel
was built. The two layers of reuse inside the guest (a vCPU's own templates and the shared pool) are designed below
and not built yet.

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

## Reuse inside the guest for 32-bit code (design; not built yet)

The numbers below (Results) say what is left after the host side: the i386 configure still makes 15.3 million
requests to the host where the 64-bit configure makes 1.06 million, because 64-bit code has two further layers that
32-bit code is shut out of. Both live in the guest-side runtime and both end in the same copy routine:

- **a vCPU's own templates**: a block whose answer depended only on its own pages' bytes is marked a template; when
  the same physical page turns up at another address (a library mapped by the next process), the runtime copies the
  block and rewrites its addresses, without asking the host;
- **the shared pool**: the host publishes every template into a read-only ring all vCPUs map, found by content, for
  the vCPU that has no template of its own.

The host switches both off for 32-bit code with one condition (`template = ... && !compiler.bits32`). The design is to
remove that condition and make the three places that assumed 64-bit code right for 32-bit. No new structure.

**(a) The key, and the x87 guess.** The pool and the template table are keyed by the code context, the page offset
and the bytes. The code context already holds the code mode and the four null-segment bits, so 32-bit and 64-bit
entries, and entries under different null segments, are apart today. What the context lacks is the x87 guess.

fexc reads the guess only when compiling a unit that contains x87 instructions (the pass that uses it returns at once
otherwise), and reports the read. So the rule is: **a 32-bit unit whose compile read the guess is not a template.** It
is neither copied by the runtime nor published; each process asks the host for it, as now, and the host serves it from
its kept answers. Every other 32-bit unit is a template exactly as a 64-bit one is, with the pool's key unchanged.
This is exact: a unit that did not read the guess is the same code under every guess, by fexc's own report (and the
host fails a request whose compile read anything outside its key).

Why not carry the guess in the pool's key instead (the tag the answers table uses): it would need the guess in each
block's record in the runtime, a second probe per lookup, and a wider pool key, to cover x87 units only. The numbers
do not ask for it: at most 5.8% of this workload's compiles were of bytes compiled before, for all causes together,
so guess-specific units are a small part. If a workload of many short x87-heavy processes shows otherwise, the guess
can be added to the key then. The pool's per-context gating note is untouched: it takes fexc's 15 gating arguments,
which hold no guess, and one context's 32-bit requests agree in them as 64-bit ones do.

For this the host keeps the guess's read bit with each kept answer (it is dropped today, because the tag holds the
guess): `template` becomes "... and not (32-bit and the answer read the guess)".

**(b) The copy routine.** The runtime rewrites a copied block with `relocate_code`, the 64-bit rule. The mode-aware
form (`relocate_code_mode`) exists since this change's host side; the runtime's two calls (a copy of an own template,
a copy from the pool) pass "32-bit" from the block's code context. For a 32-bit block that means the 32-bit move form
only and nothing leaving [0, 4 GiB), as on the host; a refusal sends the request to the host, as any refusal does.

The 64-bit path: its two calls pass `false` where today the wrapper does, so the code it runs is the same function
with the same argument; the only addition is reading the mode from the context once per copy, on the miss path, never
in translated code. Shown by: the 64-bit unit tests of the rule unchanged; `fexc-replay` (no 64-bit output change);
and the 64-bit configure and make of the timed build with their `reuse` and `pool` profile shares, before and after
(58.3 s and 89.1 s now).

**Three places that assume 64-bit code, and what happens to each:**

1. *Position-independent sharing.* For 64-bit user code the runtime does not always copy a template: it can rewrite it
   once into a form that reads its page's address at run time and share that code between addresses. The rewrite
   needs moves of at least two or three words and adds in 64 bits; 32-bit moves are one or two words and wrap at
   4 GiB. 32-bit contexts are excluded from sharing (one mode test where a template is about to be shared): they are
   copied. Sharing them would need fexc to emit wider moves in 32-bit code, a change of its output; not in this change.
2. *Blocks the runtime has modified.* A 32-bit block that begins with the test for an open x87 run has its code changed
   in place by the runtime when it is split into hot and cold parts. The copy routine already refuses such a block as a
   source; the request then goes to the pool, which holds the host's unmodified code, and the copy from the pool is
   split afresh. No change, but a test must show it.
3. *The x87 state tables.* The per-instruction x87 maps and the page-crossing probes are records beside the code, keyed
   by offsets inside the block; both copies carry them along unchanged, as they carry every other record. The table of
   addresses whose guess varied concerns only units compiled for a guess, which are not templates.

**Proof.** The runtime's own reuse check exists (`NATEMU_CHECK_REUSE=1`): every block the runtime copies, from a
template or from the pool, is also requested from the host and compared in full (code, records, link points, exits,
pages, relocations), and a mismatch stops the run. With it: the `i386` campaign at 1, 2 and 4 vCPUs, `torture32`,
`libm32`. Unit tests: a 32-bit template copied to another address equals the host's answer there; a unit that read the
guess is not a template and is never published; a block with the run test is copied from the pool, not from the
modified template; 64-bit and 32-bit entries of the same bytes and address do not meet in the pool.

**Measure after:** one profiled and one timed `io5-solo32`, one `startup32`.

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

Measured November 22, 2026 on the M4, alone on the machine, one run each, after the rebase onto Checkpoint B
(m2-32bit 78f60965). "Before" is [Checkpoint B's](m2-checkpoint-b-findings.md) run of the same cases.

Times of the build (`io5-solo32`, 4 vCPUs, seconds):

| Step | before | after | QEMU |
| --- | ---: | ---: | ---: |
| 64-bit configure | 58.8 | 58.3 | 94.9 |
| 64-bit `make -j4` | 89.6 | 89.1 | 357.6 |
| i386 configure | 374.2 | **149.2** | 110.9 |
| i386 `make -j4` | 223.2 | **126.3** | 464.0 |

The i386 configure is 2.5 times faster and the make 1.8 times. Building for i386 now costs natemu 2.6 times the 64-bit
configure and 1.4 times the make (before: 6.4 and 2.5; real x86 and QEMU: 1.2 to 1.3). The configure is still slower
than QEMU's (0.74 of its speed, from 0.30).

Profile (`io5-solo32-profile`; `tests/io/io5_compiles.py`), shares of vCPU time that is not halted:

| Step | waiting for the host, before | after | in translated code, after | installing blocks (link, place, patch), after |
| --- | ---: | ---: | ---: | ---: |
| i386 configure | 69% | **34%** | 17% | 22% |
| i386 make | 49% | **12%** | 60% | 10% |

Compiles in the part of the i386 configure the request log covers (its first 62%, as before):

| | before | after |
| --- | ---: | ---: |
| translation requests | 14.3 million (all compiled) | 15,307,090 |
| compiles | 14,344,170 (182.4 s) | **98,733 (1.8 s)** |
| of bytes not compiled before in the boot | 91,365 | 91,015 |
| of bytes compiled before | 14,019,850 | 5,771 (0.1 s) |

So compiling is gone from the profile: 99.3% fewer compiles, and what is compiled is new code. What is left of the wait
is the request itself. Each process still asks the host for every block it runs, 15 million times in this part of the
configure; the host answers from its kept answers in about 1.4 microseconds, but the round trip from the guest and
back, and installing the copy, cost more than that: 34% of the step waiting and 22% installing. The 64-bit configure
makes 1.06 million requests for the same work, because there the template pool serves copies inside the guest without
asking the host. The pool for 32-bit code is the next step for this workload (see Not done).

**The guess in the key.** The x87 guess is part of every 32-bit entry's tag, so a block without x87 instructions is
compiled once per guess value it is asked for under. The 5,771 compiles of bytes compiled before are the upper bound
of that cost (they also hold refused relocations, single-step requests and other gate values): 5.8% of the compiles
and 0.1 s of a 165-second step. The 64-bit configure, which has no guess, compiles bytes again at the same rate
(6,788 of 119,218, 5.7%). Left as it is; a two-step lookup (without the guess first) would save at most that 0.1 s.

`startup32`: not measured yet (it will be measured once, after the pool works for 32-bit code).

Correctness, under the reuse check (`NATEMU_CHECK_REUSE=1`: every answer used again is compiled again at that address
and compared; a mismatch stops the run), on the M3 before the rebase: the `i386` campaign at 1, 2 and 4 vCPUs (132
passed, 12 expected failures), `torture32` and `libm32` without AFP in fast mode (4372 passed, 499 expected failures).
The fexc suite (276 cases, with `fexc-replay`: no change to 64-bit output, `fexc-asm32` and `fexc-precision-32`) and
the Rust unit suite passed there too. After the rebase: the Rust unit suite (629 passed) on the M4; `libm32` is still to rerun (its recipe changed on the base).

## The alternative: one block for every address

QEMU keys its translated blocks by guest physical page and CPU flags, and with position-independent blocks
(`CF_PCREL`) one block serves every virtual address without patching: the block reads the current PC from CPU state
where it needs it. natemu instead keeps one answer per (bytes, page offset, mode) and makes a patched copy per address.
natemu has part of this already, for 64-bit user code: the runtime can rewrite a template once into a form that loads
its page's address from CPU state and adds an offset, and share that one piece of code between addresses instead of
copying it (the position-independent sharing in the design section above). It is not the keying QEMU uses (blocks are
still found by address and by physical page), and it does not cover 32-bit code, whose address constants are too
short for the rewrite and wrap at 4 GiB.

What a copy costs here: a table lookup, rewriting a handful of move instructions, and placing the block again in the
guest-side cache. Whether copies of 32-bit code cost enough to extend sharing to it is a question for the numbers
after the pool works for 32-bit code: then the host is out of the path, and what remains of "installing blocks" (22%
of the i386 configure today, with the host round trip gone it will be the largest part) is the copy itself. If it
stays that large, sharing for 32-bit code is the next step; it needs fexc to emit fixed-width address moves in 32-bit
code.
