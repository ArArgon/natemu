# INV-01-12 — integrated mechanism prototype

Experiment code for `INV-01-12`, answering `Q-01-12` and testing kill assumption
`KA-11` (and, in the narrow senses stated below, `KA-06` and the explicit-check
branch of `KA-04`).

Prototype code exists to test a mechanism and is discarded after Track 01. It is
not the emulator, not a design, and produces no performance result. Nothing here
is evidence until the method and code gates pass and an evidence run produces a
manifest.

## What it does

One host process runs two vCPU threads over one shared 16 MiB guest-physical
memory. Each vCPU dynamically translates a documented x86-64 subset into arm64
inside its own `MAP_JIT` region and executes the generated code. A synthetic
x86-64 guest program builds its own 4-level page tables and IDT, brings up the
second vCPU, and then exercises, in one run:

| Mechanism | How the run exercises it |
| --- | --- |
| Translation | Every guest instruction executed is translated arm64 in a `MAP_JIT` region; the code cache is keyed by guest-physical address, per vCPU. |
| Guest paging | A software MMU walks the guest's own tables, fills a per-vCPU software TLB, sets accessed and dirty bits with a locked read-modify-write, and honours `invlpg` and `mov cr3`. The guest reads the A/D bits back out of its own PTEs and the checker asserts them. |
| Precise exceptions | Four page faults (not-present read, not-present write, write to read-only, and a stack `push` whose store is not present) and one `#UD` are delivered through the guest's IDT with the x86-64 frame; `#PF` uses an IST slot so a fault on the guest stack does not nest. The guest handler repairs the mapping and `iretq` returns to the faulting instruction, which then completes exactly once — including its effect on RSP. |
| Interrupts | A host timer thread posts ticks to vCPU 0 under a post-on-EOI discipline; vCPU 1 posts IPIs through a LAPIC-like ICR register. Delivery happens at translated-block boundaries and out of `hlt`, and respects `IF`. |
| Two concurrent vCPUs | A two-party sense-reversing barrier both vCPUs must be simultaneously live to pass, a contended `lock xadd` counter, a message-passing pattern in both directions, and overlapping execution windows stamped from a single host clock. |

Self-modifying code is exercised separately: the guest rewrites the immediate of
an instruction on a page whose translation is already cached, and the new
behaviour must be observed together with a recorded code-cache flush.

## Layout

```
build.sh                 assemble the guest, generate guest_blob.h, build and sign
run.sh                   evidence run; refuses without NATEMU_RAW_ROOT
prototype/proto.c        translator, software MMU, exceptions, LAPIC, vCPU threads
prototype/guest.s        the x86-64 guest program
prototype/jit.entitlements  com.apple.security.cs.allow-jit, applied ad hoc
src/elfextract.py        ELF64 .text and symbol extraction (no llvm-objcopy on this host)
src/check.py             invariant checker; PASS/FAIL, non-zero exit on FAIL
```

Nothing is generated inside this directory. `build.sh` writes `guest.o`,
`guest_blob.h` and the binaries to its output directory; `run.sh` puts that
directory under the raw-data root.

## Build and run

```sh
./build.sh /some/scratch/dir            # prints the binary path
/some/scratch/dir/natemu_proto --out result.json
python3 src/check.py result.json        # PASS / FAIL, exit 0 / 1

export NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw
./run.sh                                # positive run plus every control
python3 src/check.py --selftest         # the checker's own check
```

`run.sh` refuses to run unless `NATEMU_RAW_ROOT` is set, is an existing
directory, and lies outside the repository, and writes only under
`$NATEMU_RAW_ROOT/INV-01-12/<UTC timestamp>-<git short rev>/`.

## Negative controls

Each is a flag on the same binary, so the control exercises the real checker.
Every control must produce a completed run whose JSON `src/check.py` marks FAIL
with a non-zero exit.

| Flag | What it breaks | Targeted invariants | Determinism |
| --- | --- | --- | --- |
| `--inject-push-order` | `push`/`call` commit RSP before the store that can fault | `I6d`, `I6e` | deterministic |
| `--inject-skip-restart` | `iretq` resumes after the faulting instruction instead of at it | `I5d`, `I6a`–`I6c` | deterministic |
| `--inject-nonatomic-rmw` | `lock xadd` emitted as load/add/store instead of `LDADDAL` | `I3` | near-deterministic (interleaving over 200 000 contended increments) |
| `--inject-drop-ipi` | every third IPI is discarded by the LAPIC model | `I11a`, `I11c` | deterministic |
| `--inject-stale-cache` | the store-side self-modifying-code check is suppressed | `I8b`, `I8c` | deterministic |
| `--inject-no-barrier` | ordinary loads and stores emitted as `LDR`/`STR` instead of `LDAR`/`STLR` | `I4a`, if a violation surfaces | **stochastic** |

`run.sh` passes each control's targeted invariants to `check.py --expect`, which
labels them in the output and fails the run if a targeted invariant did **not**
fire. Everything else a control breaks is **collateral and is not a witness**:
`--inject-skip-restart` corrupts the guest and then aborts, so it also trips
`I1a`, `I2b`, `I2c`, `I2f` and most of phase three; `--inject-drop-ipi` starves
the peer through its bounded acknowledgement spin, so it also trips `I4b`
(about 21 message-passing spin timeouts) and inflates the concurrent phase from
roughly 60 ms to roughly 8 s, which distorts the concurrency instrument in that
run; and every control trips `I2i` because `I2i` asserts that no injection was
active. None of those is evidence about the mechanism the control targets.

The no-barrier control is stochastic by predeclaration: arm64 need not exhibit
the reordering in any given window, so its non-detection is not evidence that
the acquire/release emission is unnecessary, and it does not carry the code
gate. The four deterministic controls and the near-deterministic one carry it.

`--inject-skip-restart` additionally aborts later in the run, because a guest
whose faulting instructions were skipped is in an inconsistent state. The
invariants the control targets are recorded in the JSON before that happens, so
the targeted verdicts — not the abort — are what demonstrate the oracle.

## Deliberate simplifications

Each cuts a real corner. The argument that it does not weaken the mechanism
oracle is given per item, and a reviewer may reject any of them individually.

| Simplification | Why it does not weaken the oracle |
| --- | --- |
| **Flat addressing only.** No GDT, no segment limits or bases; `CS`/`SS` are fixed selector values pushed and popped by the interrupt frame but never checked. | The mechanisms under test are translation, paging, fault delivery, interrupts and concurrency. Segmentation is inert in 64-bit mode for the data segments a Linux guest uses, and checking selectors would change no path exercised here. Segment semantics are owned by `Q-02-03`. |
| **No FPU, SSE, or vector state.** | No mechanism under test touches FP state. Adding it would grow the register-save surface without exercising a new mechanism. Guest CPU state is owned by `Q-02-03`. |
| **64-bit mode only; no 32-bit or compatibility mode, no real mode.** The guest starts with paging off and enables it itself. | The envelope is direct 64-bit boot (`INV-00-02`); a mode switch would exercise a boot path, not a mechanism. |
| **ALU instructions are 64-bit only; `mov` supports 1/2/4/8 bytes.** | The guest needs 32-bit `mov` for the LAPIC registers and a byte/dword store for the code patch; nothing in the mechanism set depends on 32-bit arithmetic. |
| **Shift instructions do not update flags.** | x86 leaves the flags of a zero-count shift unchanged and `CF`/`OF` are set from bits shifted out. The guest program never branches on flags set by a shift (the shifts are page-table index arithmetic), which can be verified by reading `guest.s`. A guest that did would need the full model; that is a conformance question owned by `Q-02-01`/`Q-02-03`. |
| **The accumulator-immediate ALU short forms (`05`, `25`, …) are not decoded.** | The subset is closed under what the guest program needs, not under the ISA. The guest uses the ModRM forms instead. |
| **`movs` and the other string instructions are excluded.** | `rep movs` adds `DF`, `RSI`/`RDI` and repeat semantics without touching any of the five mechanisms. `mov` with full 64-bit ModRM/SIB/RIP-relative addressing *is* implemented, which is what the memory path needs. |
| **Memory operands that straddle a page boundary raise `#GP` instead of being split.** | Splitting an access is a real translator obligation, but it is a second address-translation path, not a different mechanism. The three fault classes are exercised with non-straddling operands, and the checker fails the run if a `#GP` ever fires (`I7b`), so the simplification cannot hide a defect — it converts one into a failure. Routed to `Q-01-06`. |
| **The address-translation step of every guest memory access is a helper call; the access itself is an inline `LDAR`/`STLR` (or `LDADDAL`/`CASAL`/`SWPAL`) in the generated arm64.** | For accesses the *guest program* performs, the ordering-bearing instruction is in the translated code, which is what the `KA-06` emission rule is about. Three classes of guest access are performed by helpers instead and are an explicit exception to the rule: the page-table walk in `walk()`, whose entry load is an acquire load and whose accessed/dirty update is a locked `fetch_or`, so it is ordered and atomic though not emitted; the interrupt-frame push and pop in `gpush`/`gpop`, which run with the vCPU's own guest state quiesced inside `deliver`/`hiretq` and are not concurrent with guest execution on that vCPU (they are concurrent with the *peer*, which by construction never reads another vCPU's stack frame); and the `lidt` operand read in `hlidt`, a one-time setup read of a location no peer writes. Each is named so the `KA-06` statement is not read as covering them. |
| **Guest accesses must be naturally aligned.** On this host `LDAR`/`STLR` and the LSE atomics are defined only within a 16-byte granule, and an 8-byte access crossing one raises a host alignment fault. x86 permits arbitrary unaligned accesses, so this is a real divergence. | An unaligned access raises `#GP`, which `I7b` turns into a run failure rather than a hidden defect or a host `SIGBUS` — the same conversion used for page-straddling accesses. The self-modifying-code patch is a one-byte store, so it is aligned by construction rather than by accident of layout. `CLM-01-12-10` is bounded to naturally aligned accesses; unaligned-access support is routed to `Q-01-06`. |
| **The `LOCK` prefix is honoured only on `0F C1` (`xadd`) and `0F B1` (`cmpxchg`); `87` (`xchg`) is atomic without it. `LOCK` on any other instruction is rejected at translation.** | Rejection makes the gap a visible unsupported-instruction abort rather than a silently non-atomic sequence that no invariant would see. The guest uses only the three atomic forms. |
| **`#PF` is delivered on a fixed per-vCPU IST1 stack rather than one read from a TSS, and no TSS or GDT exists.** | The IST mechanism is what makes a fault on the guest stack deliverable without nesting, which is the property the push-restart test needs. Where the pointer comes from is a machine-description question owned by `Q-01-13`; using a fixed per-vCPU address changes nothing about the delivery or restart mechanism. |
| **The locked read-modify-write forms are 64-bit only.** | The guest needs no 32-bit locked operation, and a 32-bit form would require a separate flag-computation path whose only effect would be on `CLM-01-12-06`'s discrimination — which is exercised at 64 bits. |
| **Block chaining is not implemented; every translated block returns to the dispatch loop.** | Chaining is a performance mechanism. Its absence makes interrupt checks at block boundaries strictly more frequent, so it cannot mask a delivery defect. It does mean nothing here bounds interrupt latency under chaining, which a later design must. |
| **The code cache is per vCPU and never freed; a self-modifying-code flush drops entries rather than reclaiming memory.** | A single run allocates a few hundred blocks. Reclamation is a resource question, not a mechanism, and is owned by `Q-01-14`. |
| **The self-modifying-code flush is unscoped in this guest.** The whole guest program is one 4 KiB page, so a page-scoped flush and a whole-cache flush are indistinguishable here. | `I8b` still establishes that the modified instruction executed with its new behaviour and `I8c` that a flush was recorded on the vCPU that performed the store, which is what the composition question needs. Granularity — the `KA-07` question — is not established and is routed to `Q-01-05`. |
| **Self-modifying code is detected by a store-side page check, not host write protection.** | Both are named as admissible in `KA-07`; the store-side check is the smaller of the two and exercises the retranslation path identically. Which mechanism a design should use, and at what granularity and cost, is owned by `Q-01-05`. |
| **Cross-modifying code (one vCPU writing code another is executing) and cross-vCPU TLB shootdown are out of scope.** | These are genuinely unexercised. They are stated as limitations, not covered by any invariant here, and routed to `Q-01-05`, `Q-01-08`, and `Q-01-18`. No claim from this record may lean on them. |
| **The LAPIC model has five registers (ID, EOI, ICR, LVT timer, timer enable) and no real APIC semantics.** | The oracle counts posted, delivered and acknowledged interrupts; a fuller APIC changes the device contract, not the delivery mechanism. The device contract is owned by `Q-01-13`. |
| **`sti` takes effect at the next block boundary rather than after exactly one instruction.** | The x86 one-instruction interrupt shadow is a delivery-timing detail. The `IF`-gating invariant (`I10a`) still fails if `IF` is ignored altogether, which is the property under test. Exact shadow semantics are owned by `Q-02-03`. |
| **`int3` and `#GP` delivery paths exist but the positive run never takes them.** | They are wired so that an unexpected trap is visible; the checker requires `gp_count == 0`. They are declared unwitnessed rather than counted as passing. |
| **The guest is a synthetic assembly program, not a Linux kernel.** | The question is whether the mechanisms compose, not whether a guest boots. Nothing here establishes anything about the `Q-00-02` compatibility boundary, direct boot, or the `Q-01-13` machine contract. |

## Invariant witnesses

`src/check.py` makes roughly forty assertions grouped as `I1`–`I15`. Each is
either broken by a runtime negative control, or by a `--selftest` mutation, or
declared unwitnessed. Nothing is counted as passing merely because it passed.

| Invariant | Witnessed by |
| --- | --- |
| `I1a` abort, `I1b` timeout | `--selftest` mutation (`abort`, `timed_out`); collateral in `--inject-skip-restart` |
| `I1c` nested fault | `--selftest` mutation |
| `I2a`–`I2f` vCPU liveness and completion | `--selftest` mutations; collateral in `--inject-skip-restart` |
| `I2g` code cache reused | `--selftest` mutation (`blocks = 5`) |
| `I2h` `MAP_JIT`, `I2i` injection, `I2j` `loop_k`, `I2k` `loop_n`, `I2l` `nvcpu`, `I2m` timer period | `--selftest` mutations; `I2i` additionally fires on every control run |
| `I3` shared counter | **`--inject-nonatomic-rmw`** (targeted) and a `--selftest` mutation |
| `I4a` message-passing violation | `--inject-no-barrier`, **stochastic**; a `--selftest` mutation covers the checker logic. No deterministic control breaks it. |
| `I4b` spin timeouts, `I4c` iteration counts | `--selftest` mutations; `I4b` collateral in `--inject-drop-ipi` |
| `I5a` fault count, `I5b` error code, `I5c` CR2 | `--selftest` mutations |
| `I5d` pushed RIP | **`--inject-skip-restart`** (targeted) and a `--selftest` mutation |
| `I6a`–`I6c` restart exactly once | **`--inject-skip-restart`** (targeted) and `--selftest` mutations |
| `I6d`, `I6e` push restart and RSP commit | **`--inject-push-order`** (targeted) and `--selftest` mutations |
| `I7a` `#UD`, `I7b` `#GP` | `--selftest` mutations. No control raises a real `#GP`; `I7b` is the failure channel for the alignment and page-straddle simplifications. |
| `I8a` pre-patch value | `--selftest` mutation |
| `I8b` post-patch value, `I8c` flush on the storing vCPU | **`--inject-stale-cache`** (targeted) and `--selftest` mutations |
| `I9a` post-on-EOI identity, `I9b` EOI count, `I9c` tick floor | `--selftest` mutations |
| `I10a` `IF` gating, `I10b`/`I10c` `HLT` wake | `--selftest` mutations |
| `I11a` IPIs received, `I11c` acknowledgement timeouts | **`--inject-drop-ipi`** (targeted) and `--selftest` mutations |
| `I11b` IPIs dropped, `I11d` IPIs sent | `--selftest` mutations |
| `I12a`, `I12b` compare-and-exchange | `--selftest` mutations; collateral in `--inject-skip-restart` |
| `I13a` window overlap, `I13b` parallelism floor | `--selftest` mutations (disjoint windows; halved thread CPU time) |
| `I14` faults raised from translated code | `--selftest` mutation |
| `I15a`–`I15c` accessed/dirty bits | `--selftest` mutations |

**Unwitnessed at runtime:** `I4a` has no deterministic control, so the
necessity of the acquire/release emission rule is not established at any
iteration count. `I7b` has no control that raises a `#GP` on purpose. Every
other invariant is broken either by a control or by a self-check mutation.

## Concurrency instrument

`I13a` (the marked windows overlap) only shows both threads were alive across
the concurrent phase; a preemptively time-sliced single core would satisfy it.
`I13b` is the instrument that separates the two: each vCPU stamps its own
thread's consumed CPU time (`thread_info`, `THREAD_BASIC_INFO`) at the same
exit-to-host points that stamp the wall clock, and the checker requires

    (vCPU 0 thread CPU time + vCPU 1 thread CPU time) / wall span >= 1.50

Two threads genuinely running on two cores across the phase give a ratio near
2.0; one core interleaving them gives a ratio near 1.0. The floor is
predeclared in the method and frozen before any evidence run.

## ThreadSanitizer

`build.sh` produces a second binary with `-fsanitize=thread` and `run.sh` runs
it once per evidence run. Any race report is a code-gate failure and makes
`run.sh` exit non-zero. Guest memory accessed by generated code is not
instrumented, so this covers the C helpers, the device model, and the shared
page-table walk — which is where the races that matter to this record live.

## Known non-results

Wall time is recorded to characterize the run for its manifest. It is **not** a
performance result: it may not enter `Q-00-04` aggregation, `Q-04-01` cost
modelling, or any comparison with the `Q-00-03` baseline.
