# M2: sharing translated 32-bit code between addresses

Branch `pic32` (FEX branch `natemu-pic32`, 4eba851 on 9554e5b), on `m2-32bit` at 9cd51ca6 (32-bit reuse and the shared
store probes). November 22, 2026.

Since [32-bit reuse](m2-reuse32-findings.md) a 32-bit process got its blocks without asking the host, but each one was
still a patched copy made for that process's own addresses. A 64-bit process does not copy: position-independent user
code is rewritten once into a form that reads its page base at run time, and every address shares that one
translation. In the i386 configure the copies, and the linking and patching that come with them, cost 31.9 s of vCPU
time, against 6.3 s in the 64-bit configure. This branch shares 32-bit user code the same way.

**Result** (M4, 4 vCPUs, one run each, seconds). "Before" is this branch's base without sharing, `m2-32bit` at
9cd51ca6, in bench case `io5-solo32-profile`; its step times match eedae4a2's timed `io5-solo32` (the 32-bit reuse
findings' run: 57.5, 90.0, 106.7, 114.7), so the shared store probes in between did not move them. "After" is this
branch at 3fd3b398, timed (`io5-solo32`) and profiled (`io5-solo32-profile`):

| Step | before (9cd51ca6, profiled) | after, timed | after, profiled | QEMU |
| --- | ---: | ---: | ---: | ---: |
| 64-bit configure | 56.9 | 58.0 | 55.9 | 94.9 |
| 64-bit `make -j4` | 89.8 | 89.5 | 89.5 | 357.6 |
| i386 configure | 106.9 | **82.5** | 82.1 | 110.9 |
| i386 `make -j4` | 115.1 | **108.1** | 108.9 | 464.0 |

The gain is sharing's alone. The i386 configure is 23% faster: 1.42 times the 64-bit configure, where before it was
1.86 times. The make is 6% faster. The 64-bit steps did not move.

## The design

In short:

- **One move form in 32-bit code.** fexc (FEX's `InsertGuestRIPMove`, under `fexc_bits32`) now builds every guest
  address in 32-bit code with exactly three words: `movz wR, #lo; movk wR, #hi, lsl #16; nop`, the value masked to 32
  bits. Before, it used one or two words depending on the value. 64-bit output is unchanged.
- **The shared form.** The resident rewrites each move in place into `ldr xR, [x28, #PAGE_BASE]; add|sub wR, wR, #hi,
  lsl #12; add|sub wR, wR, #lo`. The two 32-bit adds wrap at 4 GiB exactly as fexc's masked value does. So the form
  is exact at every page base below 4 GiB, also where a target wraps past 4 GiB, and reaches ±16 MiB (`pic.rs`,
  `bits32` mode of `form`, `value`, `eligible`, `rewrite`, `link_point`, `proved` and `moves_agree`).
- **Where a 32-bit instance may be.** An instance must lie below 4 GiB, the copy rule's test (`in_32_bits`), instead of
  the 64-bit canonical-edge test. Every place that moves a shared translation's link target to the address it runs for
  does it modulo 2^32, through one helper (`pic::moved`). There are five such places: the instance, the link's match,
  the linker, the INT 0x80 fast path, and a stop at a link point.
- **Host.** The relocation rule takes the three-word form alone, the `nop` included. Admission accepts a `nop` only as
  a 32-bit move's third word and refuses an unpadded move.
- **The switch.** `NATEMU_PIC=user64` (`PIC_USER64`) turns 32-bit sharing off for an A/B comparison.
- **Left out.** As in 64-bit code, these stay unshared: blocks with the x87 run test (`share` refuses them), units
  compiled for an x87 guess (not templates), and global mappings. Shared code gets no hot/cold split.

## Reviews

- **The design review** added a fifth place that moves link targets: a stop at a link point (`stopped` in
  `execution.rs`). It also asked that the replay comparison decode PC-relative words across moves, and that
  eligibility be measured before the gain is claimed.
- **The code review** found two holes, both fixed:
  - **Admission.** The check that nothing reads an FMA check's result register stopped scanning at the new `nop`, so
    a read after a 32-bit move went unchecked. The scan now goes on past it.
  - **The replay comparison.** `only_moves` checked a PC-relative word's target only when the word itself changed, so
    a branch over a move whose words fexc left unchanged would have passed. It now checks every such word. A unit test
    (`tests/static/test_replay_only_moves.py`) pins it with synthetic replies: a two-word move against its three-word
    twin, a `cbz` over it; it fails under the old rule. The replay's own mutations (a branch over a move shortened by a
    word) catch it only where OLD and NEW differ: in the libc corpus against the old compiler, the old rule missed 1,100
    of them and the new one none, but in `fexc-replay-order`, which compares a compiler with itself, no word moves.
- **Two smaller points, also fixed:** the run address of 32-bit shared code is masked to 32 bits, and the
  self-checking replay case now runs the 32-bit comparison.
- **One point left open:** the 64-bit `link_point` also accepts a `nop` before `svc` after a 32-bit move form. fexc
  never emits that in 64-bit code, and admission still refuses it there.

## Results

### Which blocks can be shared, and what the form costs

An experiment, not a framework case. `pic32_survey`, an ignored host test, compiles an i386 ELF block after block as
the host does for 32-bit user code and counts what `share` would accept. Its inputs are libc and libm from the
campaign-i386 image, plus `python3.13-i386`, a program that runs at one fixed address. `replay.py --old32` then
answered the same requests with fexc at 9cd51ca6 (the old forms) and at this branch, compared every reply with
`only_moves` and summed code bytes. Run on the M4, November 22.

| Input | blocks | templates | shareable | not shareable | code bytes, old to new |
| --- | ---: | ---: | ---: | --- | ---: |
| libc.so.6 at 0xf7c00000 | 20,000 | 19,734 | 19,734 | none | +4.82% |
| libm.so.6 at 0xf7a00000 | 17,325 (all) | 10,041 | 10,011 | 30 with the x87 run test | +2.72% |
| python3.13-i386 (fixed address) | 20,000 | 19,969 | 19,969 | none | **+7.08%** |
| the replay gate's 32-bit requests | 82 | | | | +2.80% |

- No template was refused for a move out of reach, and admission refused no block.
- Every reply differed from the old compiler's only by the move form (or not at all). Every mutation of a passing
  reply was reported.
- Libraries grow by 3 to 5%. The fixed-address program grows by 7%, because its blocks are small (130 bytes on
  average) and each carries 1.75 moves. Together the corpus grows by 4.3%.
- No large program was surveyed. A program whose text spans more than 16 MiB (perhaps the i386 `cc1`) could have
  moves out of the form's reach.

### Where the time went

The profiles (bench case `io5-solo32-profile`, M4; `tests/io/io5_compiles.py`) give busy vCPU time in the i386
configure, in seconds summed over the 4 vCPUs. "Before" is the base, 9cd51ca6, without sharing.

| Bucket | before | after | 64-bit configure, after |
| --- | ---: | ---: | ---: |
| all busy time | 112.6 | 87.2 | 59.5 |
| in translated code | 27.7 | 27.8 | 22.7 |
| copying ("reuse") | 14.9 | **1.2** | 1.5 |
| delivery | 14.4 | **21.7** | 3.2 |
| linking | 13.1 | **4.2** | 4.2 |
| pool lookups | 6.9 | **2.2** | 2.2 |
| patching | 4.3 | **1.0** | 0.8 |
| requests to the host | 977,045 | 748,199 | 1,062,978 |

- Copying, linking, patching and pool lookups in the i386 configure are now the 64-bit configure's: 8.6 s, where
  before they were 39.2 s. In the i386 make they went from 55.2 s to 24.5 s.
- Over the whole boot, the resident shared 2.08 million templates and made 30.5 million instances. It refused no
  instance and found no rewrite unproved. The INT 0x80 fast path never declined because of a link target.
- **Delivery grew from 14.4 s to 21.7 s.** Its cause is below.

### Why delivery grew

The guest's page faults are the same in number but each costs more to deliver. Over the whole boot (all four vCPUs):

| | before (9cd51ca6) | after |
| --- | ---: | ---: |
| page faults delivered | 11.91 million | 11.93 million |
| time in them, natemu's part | 30.6 s | 39.4 s |
| block records visited on linear chains, all callers | 345 million | 481 million |
| flushes of the code cache because it was full | 101 | 45 |

A delivered #PF counts in the delivery bucket, and in it natemu cuts every block on the faulting linear page
(`page_faulted`, `cache::invlpg`): it walks that page's hash chain of block records. With sharing the code area fills
more slowly, because an instance has no code of its own, so the cache is flushed when full less than half as often,
and the chains hold records longer. A count added to the perf build (`page_fault_scanned`, `scanned_instances`,
`scanned_stale`; one profiled run, this branch, M4) shows what they hold: of the 481 million visits, 384 million were
made while delivering a #PF (32 per fault), 399 million were of instances, and 480 million were of blocks that were
not current. These are most likely the instances left by earlier processes at the same addresses: i386 libraries load at
few distinct bases, so every new process adds instances at the same linear pages. Delivery's growth (7.3 s in the
configure, 1.3 s in the make, 0.3 s in the 64-bit steps) matches the 8.8 s more time in page faults.

So it is work, not misattributed time, and nearly all of it is wasted: a block that is not current is checked again
(walked, its frames compared) before it is entered, so cutting it at a #PF does nothing a later revalidation would
not do. Two fixes, neither built (each changes behaviour):

- Leave blocks that are not current off the linear chains: a scan drops one it meets, as it drops a dead one, and
  revalidation puts a block back on its chain. A #PF or INVLPG then walks only current blocks. This needs care where
  a block becomes current again without revalidation (L1 tables kept across context switches), if anywhere.
- Kill an instance that is not current when a scan meets it, rather than keep it: an instance has no code, so one is
  made again cheaply, and its translation stays. Simpler, but it trades revalidations for new instances and fills the
  record arena sooner; it needs measuring.

### Other benchmarks

**`startup32`** (bench case `guest-startup32`, M4, this branch, one run): 7.27 s in its second pass (7.54 s in the
first), against 7.16 s (7.59 s) before. The program loads at the same address every time, so it shares nothing. The
difference is within about 1.5%, and it may be the extra `nop` per move that unshared code runs.

### Tests

These ran through `tools/natemu-test` on the M4, at the final code unless noted.

- **Unit tests:** 634 passed.
- **The fexc static suite:** 275 passed.
  - The replay gate (`fexc-replay`): zero differing 64-bit replies. Another 10,686 differ only by the store probes,
    which its probe check allows.
  - `fexc-replay-order`, now also comparing 32-bit replies: 82 of 82 identical, as it should be (the same compiler on
    both sides).
  - The recorded boot's stream against fexc at 9cd51ca6: 46 replies differ only by moves, 36 are identical.
- **Narrow VM cases:**
  - `pic-*`, `compat32` and `m2` passed.
  - `i386` passed with `NATEMU_CHECK_REUSE=1` and without it; the run without the check exercises local links.
  - `torture32` and `libm32` passed, both under the reuse check: 6,277 passed, 1,450 expected failures.
  - `boot-i386` passed, at the code before the review's fixes.
- **New checks, each shown to fail on a wrong input:**
  - **pic.rs:** the 32-bit form at both reach edges and across the wrap both ways. Its proof fails under each
    mutation: a 64-bit add, no `nop`, one add short, the wrong register, an unmasked target.
  - **The 32-bit move rule:** it refuses a wrap, a move without its `nop`, other forms and other registers.
  - **Admission:** it refuses a move without its `nop`, with the code-shape check and without.
  - **Real fexc (two host tests):** a template shared from just below 4 GiB, where a call's target wraps, is word for
    word fexc's compile at another base, and is not with targets moved by a 64-bit add.
  - **The replay comparison:** its mutations of every reply it passes, including the new shortened branch.

## What is left

- **The cheaper move form is not built.** It would keep two words (`movz; movk`) and add the `nop` only where a value
  is beyond the two-word rewrite's reach. The always-three form costs 4.3% more code on the corpus, and 7.1% on the
  fixed-address program, which is over the 5% line set for this change. Whether to switch is the owner's decision.
- **Page-fault delivery walks stale records** (7.3 s of the i386 configure, above). Two fixes are proposed, neither
  built.
- **No framework case compares 32-bit replies with an older compiler.** `main` does not compile 32-bit code yet, so
  the replay gate's old compiler cannot answer 32-bit requests. The comparison with fexc at 9cd51ca6 above is an
  experiment. Once M2 is on `main`, the gate's old compiler answers them and `--old32 {fexc_base}` can join
  `fexc-replay`.
- **The forge tests check nothing for 32-bit code.** A forged high half of the page-base slot is ignored by the
  32-bit adds, and `run_pic.py` runs 64-bit programs only. A 32-bit process cannot write the slot, which lies above
  4 GiB.
- **Blocks with the x87 run test stay unshared** (30 of 10,041 libm templates). `share` could restore the test's
  return word in the code and in the proof, if more such blocks turn up.

## Appendix: design and review

The design note and its review were working files of the November 22 session. Their content is summarized above: the
move form, the shared form, the five places that move targets, the instance rule, admission and the relocation rule,
and the replay comparison. The review's amendments are listed under [Reviews](#reviews).
