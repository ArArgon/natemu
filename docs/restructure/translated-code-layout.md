# Translated-code layout and the five speed changes

Status: built (November 19, 2026). This page says what the translated code and the dispatcher look like and why; the
measurements are in section 7. The compiler side (FEX fork commit `ac479dd`) is pinned in `tools/fexc/build.sh`.

## 1. Why

A CPython build in a guest (`make -j4`, 4 vCPUs) spent about a tenth of its translated-code samples inside the dispatcher
and half of the samples inside a block on the block's first instruction, a fetch stall at a block boundary. Blocks were
single basic blocks of about 17 guest bytes, each carrying exits, metadata words and padding. Every `push` carried a
page-crossing check, and every 8-bit compare spent several instructions on the flags bookkeeping. Five changes address these.

## 2. The dispatcher's tables are data

Every indirect jump and return of guest code goes through a dispatcher slot, which looks the target RIP up in a table (the
"L1"). The tables used to be code: an entry was eight instructions the slot branched to, so filling or emptying one wrote
code and needed an instruction-cache synchronization (about 200 ns on an M4).

Now they are data. Table t's entry i is 32 bytes, the two ways of a set: two pairs of (RIP, address to branch to). A slot
masks the RIP, loads the first way's RIP, compares, and on a match loads its address and branches; otherwise it tries the
second way, and otherwise branches to the miss path. Filling or emptying an entry is two stores and nothing is
synchronized.

- Where: the tables sit in the resident's lookup area (`area::DL1`), 2 MiB blocks mapped read-only for guest code in the
  same second-level table as the guest-state alias, after it when they fit, else before it (`dl1_after`). The slot reaches
  table t from the guest-state register by a page count.
- The empty marker: an empty way holds `DL1_EMPTY` (bit 62) as its RIP and the dispatcher's miss path as its address. The
  marker passes the computed-exit canonical check (which compares bits 63 and 47 only), so a guest jump to exactly that
  address matches the marker and must, and does, miss like any other target.
- Known limitation: the tables are readable by guest user code, in a region that x86 treats as non-canonical (so no x86
  access can name it, but the guest's own translated code runs at EL0 and the mapping is what it is). Making them
  kernel-only, or randomizing their place, is deferred by the owner.


## 3. Extended blocks

A unit of translation used to end at the first branch. With `--ext-conds 4` fexc continues a conditional branch's
fall-through side in the same unit, up to 4 conditional branches. The unit has a single entry and grows forward only (a
backward target still starts a unit). The conditional branches become exits of the unit, so a unit is limited by the number
of link points a block may carry. So there are fewer block boundaries, fewer fetch stalls and fewer patched links.

## 4. Hot/cold layout

A block's native code is its body followed by exits (`mov x0, #rip; movk; svc` per exit), metadata words and padding.
Only the body is hot. In the resident:

- Without FEX's tail (its data words after the code) blocks are placed at 16-byte alignment instead of 64, so word 0 of an
  admitted object is the placed length.
- The words of a block up to its first exit are copied into a dense hot stream, one hot zone (12 KiB) at the start of each
  32 KiB segment, and the block is entered there. Branches out of the copy are re-encoded, and each exit's trampoline
  follows the body, so the exit still reaches the block's whole code, which stays in the segment's cold part; every exit,
  link, stop, site and resume names that cold copy.
- A block is placed whole, with no hot copy, when it has no exit to split at, when its body already reaches the end, or
  when its last word falls through into a word that is not an exit (such a block had a forged guard-stub site record that ran
  the hot zone's old content).

The effect is that the code the CPU fetches in a loop is packed together and a fetch no longer passes over exits.

## 5. One stack check per run of stack stores

A `push` needs a check that its store does not cross a page. With `--push-run` fexc keeps, along the unit's dataflow, the set
of registers known to be aligned and checks once for a run of stack stores (and the like) based on one of them. Admission rule:
the base is register x2 or above, and the set is the intersection over the
paths into an IR block (nothing at an entry or at a backward target). Anything the dataflow cannot show keeps its own check.

## 6. Narrow compares, and the admission rules added

With `--narrow-cmp` an 8- or 16-bit compare with a constant is translated with the constant inline, saving the move, the
shift and the flag bookkeeping.

The host requests the three options with every compile (`--ext-conds 4 --push-run --narrow-cmp`, `TRANSLATION_ARGS` in
`crates/host/src/execution.rs`); they are part of every request body and so of the persistent cache's key. In fexc itself
they are command-line flags.

Admission (`crates/host/src/admission.rs`) gained rules for the resident's placement: word 0 of an object must equal the
code length (the validated code region end) and be a multiple of 4.

## 7. Measurements

M4, 4 vCPUs, mitigations off; original, then now.

| Workload | Before | Now |
|---|---|---|
| CPython `make -j4` | 122.7 s | 95.0 s |
| CPython configure | 65.7 s | 57.0 s |

The 11 standing benchmarks, best of two interleaved rounds, ratio now/before, cold run then warm run:

| build | zstd | hackbench | locks | luajit | luajit-interp | mem | cray | 7zip | sqlite | avx |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.85 / 0.73 | 1.00 / 0.94 | 0.89 / 0.81 | 0.74 / 0.73 | 0.97 / 0.91 | 0.89 / 0.85 | 0.95 / 0.94 | 0.99 / 0.95 | 0.96 / 0.97 | 0.89 / 0.88 | 1.01 / 0.99 |

Geometric mean: 0.92 cold, 0.88 warm.

Known open cost: returns still go through the dispatcher, about 9 ns more than a jump-linked block.

## 8. Build notes

These are fixed behaviour of the resident, not options: `cargo build -p natemu-runtime --target aarch64-unknown-none` builds
them, with or without `perf` and the other study features. The counting-stub study `pic-count` no longer builds (a
`compile_error!` says so): its stubs would have to be mirrored in the hot stream by `split`.
