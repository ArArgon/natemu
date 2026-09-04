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
| Guest paging | A software MMU walks the guest's own tables, fills a per-vCPU software TLB, sets A and D bits, and honours `invlpg` and `mov cr3`. |
| Precise exceptions | Three page faults (not-present read, not-present write, write to read-only) and one `#UD` are delivered through the guest's IDT with the x86-64 frame; the guest handler repairs the mapping and `iretq` returns to the faulting instruction, which then completes exactly once. |
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

| Flag | What it breaks | Invariant it must break | Determinism |
| --- | --- | --- | --- |
| `--inject-skip-restart` | `iretq` resumes after the faulting instruction instead of at it | `I5d` pushed RIP, `I6a`–`I6c` exactly-once | deterministic |
| `--inject-nonatomic-rmw` | `lock xadd` emitted as load/add/store instead of `LDADDAL` | `I3` shared counter | near-deterministic (interleaving over 200 000 contended increments) |
| `--inject-drop-ipi` | every third IPI is discarded by the LAPIC model | `I11a`, `I11c` | deterministic |
| `--inject-stale-cache` | the store-side self-modifying-code check is suppressed | `I8b`, `I8c` | deterministic |
| `--inject-no-barrier` | ordinary loads and stores emitted as `LDR`/`STR` instead of `LDAR`/`STLR` | `I4a` message-passing, if a violation surfaces | **stochastic** |

The no-barrier control is stochastic by predeclaration: arm64 need not exhibit
the reordering in any given window, so its non-detection is not evidence that
the acquire/release emission is unnecessary, and it does not carry the code
gate. The four deterministic and near-deterministic controls carry it.

`--inject-skip-restart` additionally aborts later in the run, because a guest
whose faulting instructions were skipped is in an inconsistent state. The
invariants the control targets (`I5d`, `I6a`–`I6c`) are recorded in the JSON
before that happens, so the checker's verdict — not the abort — is what
demonstrates the oracle.

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
| **The address-translation step of every guest memory access is a helper call; the access itself is an inline `LDAR`/`STLR` (or `LDADDAL`/`CASAL`/`SWPAL`) in the generated arm64.** | The ordering-bearing instruction is in the translated code, which is what the `KA-06` emission rule is about. The helper resolves the host pointer and handles MMIO; it performs no ordinary access itself. |
| **Block chaining is not implemented; every translated block returns to the dispatch loop.** | Chaining is a performance mechanism. Its absence makes interrupt checks at block boundaries strictly more frequent, so it cannot mask a delivery defect. It does mean nothing here bounds interrupt latency under chaining, which a later design must. |
| **The code cache is per vCPU and never freed; a self-modifying-code flush drops entries rather than reclaiming memory.** | A single run allocates a few hundred blocks. Reclamation is a resource question, not a mechanism, and is owned by `Q-01-14`. |
| **Self-modifying code is detected by a store-side page check, not host write protection.** | Both are named as admissible in `KA-07`; the store-side check is the smaller of the two and exercises the retranslation path identically. Which mechanism a design should use, and at what granularity and cost, is owned by `Q-01-05`. |
| **Cross-modifying code (one vCPU writing code another is executing) and cross-vCPU TLB shootdown are out of scope.** | These are genuinely unexercised. They are stated as limitations, not covered by any invariant here, and routed to `Q-01-05`, `Q-01-08`, and `Q-01-18`. No claim from this record may lean on them. |
| **The LAPIC model has five registers (ID, EOI, ICR, LVT timer, timer enable) and no real APIC semantics.** | The oracle counts posted, delivered and acknowledged interrupts; a fuller APIC changes the device contract, not the delivery mechanism. The device contract is owned by `Q-01-13`. |
| **`sti` takes effect at the next block boundary rather than after exactly one instruction.** | The x86 one-instruction interrupt shadow is a delivery-timing detail. The `IF`-gating invariant (`I10a`) still fails if `IF` is ignored altogether, which is the property under test. Exact shadow semantics are owned by `Q-02-03`. |
| **`int3` and `#GP` delivery paths exist but the positive run never takes them.** | They are wired so that an unexpected trap is visible; the checker requires `gp_count == 0`. They are declared unwitnessed rather than counted as passing. |
| **The guest is a synthetic assembly program, not a Linux kernel.** | The question is whether the mechanisms compose, not whether a guest boots. Nothing here establishes anything about the `Q-00-02` compatibility boundary, direct boot, or the `Q-01-13` machine contract. |

## Known non-results

Wall time is recorded to characterize the run for its manifest. It is **not** a
performance result: it may not enter `Q-00-04` aggregation, `Q-04-01` cost
modelling, or any comparison with the `Q-00-03` baseline.
