# TSO coverage probe (slice 3, S3t)

Evidence that, inside a Hypervisor.framework VM on this M4 with Apple's TSO mode on, the access forms natemu's
generated code uses obey x86-TSO ordering and single-copy atomicity. Independent of natemu: hand-written arm64 at EL1,
no FEX, no natemu code read or run. Raw results: `/Users/liaozping/orca/natemu-raw/tso-coverage-20261027-01/`
(`summary.txt`, `results.json`, per-cell rows in `on/` and `off/`, `build/` with the generated assembly and the probe,
`mnemonics.json`, `disasm-sample.txt`, `host.txt`, `source/`).

## Method

- One process and one fresh VM per cell. Four vCPUs, one owner thread each (QoS user-interactive), 2 MiB of RAM, the
  stage-1 setup of the pair-atomicity experiment (4 KiB granule, MAIR attr 0 = normal write-back 0xff, inner-shareable,
  SCTLR 0x30d01985). `ACTLR_EL1 = 2` (the TSO enable bit natemu sets) is written by the host and read back by guest `mrs`
  on every vCPU at boot (the probe stops if the read-back differs; MMFR2.AT = 1 is also required). Control runs use
  `ACTLR_EL1 = 0`, everything else identical.
- Litmus cells (`litmus.py`, `gen.py`, `guest.S`): per iteration all threads meet at an acquire/release barrier, wait a random
  number of spins (xorshift, per thread; two timing profiles: delay up to 2^6 and up to 2^12 spins, half the iterations each),
  run the test body, publish result digits, and thread 0 collects a histogram of the outcome and re-zeroes the locations.
  The harness barrier uses `ldar`/`stlr`; the tested bodies use only the access forms below. `mnemonics.json` lists what
  each kind of body contains (ordering bodies: `ldr/ldrb/ldrh/ldur/ldurb/ldurh/str/strb/strh/stur/sturb/sturh` only).
- Programs: SB, MP, LB, WRC (3 vCPUs), IRIW (4 vCPUs), 2+2W, S, R, SB with store forwarding, CoRR, CoWW/CoWR; the same MP/SB
  programs with some accesses made by EL1 code running in an exception handler (`svc` from EL1, handler stores or loads
  through a second virtual alias of the same RAM, `eret`; handler-store-then-store-after-ERET, store-then-handler-store,
  both in handlers, handler load, with and without `dsb ish` in the handler).
- Forms: widths 1, 2, 4, 8 bytes, `ldr/str` (register base) and `ldur/stur` (unscaled offset) variants, at five classes:
  aligned, unaligned inside one 16-byte block (2 B at 7, 4 B at 6, 8 B at 5), crossing a 16-byte boundary inside a 64-byte
  line (offsets 15, 14, 12), crossing a 64-byte boundary (63, 62, 60), crossing the 128-byte line (127, 126, 124); plus
  crossing a 4 KiB page (the 128-class offsets with the location base at page offset 0xF80) for MP, LB, SB, CoRR and the
  atomicity rounds. Locations `x` and `y` are in different 128-byte lines.
- Oracle (`oracle.py`): an operational x86-TSO machine (per-thread FIFO store buffers, forwarding) and an SC machine
  enumerate the allowed outcomes of every program; self-check pins the textbook results (SB 0/0 allowed under TSO only;
  MP, LB, WRC, IRIW, 2+2W, S, CoRR, CoWW forbidden outcomes absent). It is my own model, not natemu's
  `i4v_ordering_model.py` (not read, per the independence rule). An outcome with a digit "other" (a value that is neither the
  initial value nor a value stored) is counted as a **torn** read, separately from forbidden outcomes.
- Atomicity (single-copy): "tear" rounds are the litmus program one writer stores a byte-uniform value (0x01..) over a
  zeroed location while three vCPUs each load it once or twice after the barrier; any value that mixes bytes of 0 and 1 is a
  tear. Variants: plain store, `swpal`/`swpalb`/`swpalh` store, and the **positive control**: the writer stores the same value
  as two half-width stores (`strb`+`strb`, `strh`+`strh`, `str w`+`str w`), which must tear. A second family, "storm" cells,
  has one vCPU storing alternating byte-uniform patterns 80M times (plain, `swpal`, or split) while three vCPUs poll;
  tears counted the same way.
- Mixed sizes: an 8-byte store seen by two 4-byte loads (low then high, high then low; forbidden: the later-loaded half new
  while the earlier-loaded half old); two 4-byte stores (low then high, high then low) seen by one 8-byte load (forbidden:
  the later-stored half visible without the earlier-stored half, or a value that is neither half).
- `run.py` builds (`gen.py` writes ~540 cells of assembly), signs with the hypervisor entitlement, runs each cell as its own
  process, checks results with `oracle.py`, runs the TSO-off controls, writes `summary.txt`.

```
python3 -B experiments/track03/tso-coverage/run.py --out /Users/liaozping/orca/natemu-raw/tso-coverage-20261027-01
```

Reproducible seeds: a cell's seed is derived from its index. Run took 19 minutes. Host: Mac16,10 (M4, 4 P + 6 E cores),
macOS 26.6.2, `hw.cachelinesize` 128.

## Configurations and iteration counts

| set | cells | iterations |
| --- | --- | --- |
| EnTSO on, ordering litmus (SB, MP, LB, WRC, IRIW, 2+2W, S, R, SBF, CoRR, CoWW) x width x class x {ldr/str, ldur/stur} | 539 cells in all (below) | 10^7 each (2 x 5x10^6); 4.7x10^9 trials across all litmus-engine cells |
| EnTSO on, exception-entry programs (7), w1/w4/w8 aligned, w8 unaligned-in-16, crossing 16/64 | 42 | 10^7 each |
| EnTSO on, atomicity rounds: plain, swpal, split control | per width and class | 10^7 each |
| EnTSO on, storms (80M stores, ~2x10^8-3x10^9 polled loads each) | plain, swpal, split | 10^7 loops x 8 stores |
| EnTSO on, mixed sizes (4 programs x al/u16/x16/x64) | 16 | 10^7 each |
| EnTSO OFF control: SB, MP, LB, WRC, IRIW, w1/2/4/8, aligned + unaligned-in-16, both insn forms | 70 | 10^7 each |

## Results (EnTSO on unless stated)

Forbidden outcomes of ordering programs, per form (all litmus programs of the form summed; "ord trials" counts every
program x variant trial; "tear rounds" are plain-store atomicity rounds; swpal = `swpal` writer rounds):

```
form    ord trials  FORBIDDEN  tear rounds  TORN       swpal rounds  swpal torn
w1-al   2.9e8       0          2e7          0          1e7           0
w2-al   2.2e8       0          2e7          0          1e7           0
w2-u16  2.2e8       0          2e7          0          1e7           0
w2-x16  2.2e8       0          2e7          0          FAULT (alignment, DFSC 0x21)
w2-x64  2.2e8       0          2e7          1,943,602  FAULT
w2-x128 2.2e8       0          2e7          1,154,702  FAULT
w2-pg   4e7         0          1e7          53,936     FAULT
w4-al   2.9e8       0          2e7          0          1e7           0
w4-u16  2.2e8       0          2e7          0          1e7           0
w4-x16  2.2e8       0          2e7          0          FAULT
w4-x64  2.2e8       0          2e7          2,411,053  FAULT
w4-x128 2.2e8       0          2e7          1,759,947  FAULT
w4-pg   4e7         0          1e7          124,778    FAULT
w8-al   2.9e8       0          2e7          0          1e7           0
w8-u16  2.9e8       0          2e7          0          1e7           0
w8-x16  2.9e8       0          2e7          0          FAULT
w8-x64  2.9e8       0          2e7          2,185,008  FAULT
w8-x128 2.2e8       0          2e7          1,499,499  FAULT
w8-pg   4e7         0          1e7          54,203     FAULT
```

(Full table with the storm columns: `summary.txt`.) Zero forbidden outcomes in every ordering cell of every class, including
crossing 16, 64, 128 bytes and a page. Those cells' tears (digit "other") are expected for 64-byte, 128-byte and page crossing
(the load is two memory accesses) and are the only non-clean entries.

Per program (aligned + unaligned-in-16 + 16-crossing, all widths, both instruction forms, EnTSO on): MP, LB, WRC, IRIW,
2+2W, S, CoRR, CoWW: 0 forbidden in 2x10^8 trials each (20 cells of 10^7); "cow" is the two-writer coherence program. **Allowed** weak outcomes are seen: SB 0/0 in
4.3x10^6 aligned/unaligned/16-crossing trials ("sb" rows of `summary.txt`: 1.7M + 1.3M + 1.3M), SB with forwarding likewise (so
store buffering is real, and forwarding is correct: 0 forbidden), R's TSO-only outcome 0.5x10^6.

Exception entry (7 programs: store before `svc` + handler store, handler store + store after `eret`, both in handlers,
handler load, with and without `dsb ish`): 0 forbidden in every class up to 16-crossing; the 64-byte class tears only.
Stores made after an exception entry and after ERET stay ordered with the thread's other stores (MP never fails).

Mixed sizes: 0 forbidden in 4 x 10^7 trials for each of aligned, unaligned-in-16 and 16-crossing; crossing a 64-byte boundary
shows the expected non-atomic 8-byte access (80,779 forbidden in 4x10^7).

`swpal` cells (aligned and unaligned inside one 16-byte block, widths 1/2/4/8): 0 torn in 10^7 rounds each, plus the
storms. Crossing a 16-byte boundary (or a page) takes an alignment fault (ESR 0x96000021, DFSC 0x21) in every width.

### TSO-off control

Same cells and plan with `ACTLR_EL1 = 0` (14 cells per program: w1/w2/w4/w8 aligned, w2/w4/w8 unaligned-in-16, both forms,
10^7 trials each):

| program | trials | forbidden outcomes |
| --- | --- | --- |
| MP | 1.4x10^8 | **2,887** (rate ~2x10^-5; 70-410 per cell, every cell) |
| LB | 1.4x10^8 | 0 (LB is not observed on this hardware even without TSO) |
| WRC | 1.4x10^8 | 0 |
| IRIW | 1.4x10^8 | 6 |
| SB 0/0 | 1.4x10^8 | 3.3x10^6 (allowed under TSO as well) |

The control observes MP violations, so the TSO-on zeros are meaningful: at the control's rate, the 2x10^8 aligned/unaligned/
16-crossing MP trials with TSO on would have produced ~4,000 violations. LB gives no discrimination (neither mode shows it).

### Atomicity and the positive control

- Plain stores/loads, aligned (1/2/4/8) and unaligned inside a 16-byte block (2/4/8): 0 tears in 2x10^7 rounds per form
  (both instruction forms, 5 loads per round: ~10^8 loads checked per form); also 0 in all litmus programs.
- 16-byte crossing inside one 64-byte line (offsets 15/14/12): **0 tears** in 2x10^7 rounds per width and in all litmus and
  mixed-size cells. Not observed, although the owner decision treats it as
  not guaranteed. Apple's TSO mode kept these accesses atomic in every run here.
- 64-byte and 128-byte crossing: tears in 7-11% of rounds; page crossing: tears (53,936 to 124,778 per 10^7).
- Positive control (writer stores the value as two half-width stores): tears seen, 93,353 in 3x10^7 rounds (aligned w2/w4/w8)
  and 12,640 (unaligned in 16); in the storm form 1.4x10^8 to 2.7x10^8 torn polls per cell. So the reader and classifier do
  detect a non-atomic write at this timing.
- Storms with plain stores to one line give few observable transitions (tens to ~4x10^4 per 80M stores: the writer keeps the line),
  so the storm columns alone would be weak evidence; the rounds, which re-fetch the line after each re-zeroing, carry the
  atomicity argument. The `swpal` storms see 2x10^3 to 3x10^7 transitions and 0 tears.

## Verdict per form

| form | verdict |
| --- | --- |
| aligned 1/2/4/8, `ldr/str` and `ldur/stur` | ordering holds (0 forbidden), atomic (0 tears), control conclusive |
| unaligned 2/4/8 inside a 16-byte block | same |
| `swpal` aligned and unaligned inside 16 bytes | atomic (0 tears), ordered like a store |
| store after exception entry / ERET (second alias) | ordering holds, 0 forbidden |
| mixed sizes (8-byte store vs two 4-byte loads; 4-byte stores vs 8-byte load) | holds, 0 forbidden |
| crossing 16 bytes inside a line | report: no forbidden outcome and **no tear observed**; `swpal` there faults |
| crossing 64/128 bytes, a page | not atomic (tears), ordering outcomes never forbidden when the torn digit is set aside (mixed sizes across 64 bytes: 80,779 forbidden); `swpal` faults |

## Caveats

- Statistical evidence on one machine (M4, macOS 26.6.2), with HVF threads not pinned; zero observations in N trials bound a
  per-trial rate near 3/N (1.5x10^-8 at 95% for 2x10^8 trials) but are not a proof. LB cannot be discriminated (no violation
  with TSO off either); with TSO off IRIW showed 6 and WRC 0 violations, so their zeros are weaker evidence than MP's.
- The oracle is my own enumerator, not `i4v_ordering_model.py` (independence rule); the two were not compared.
- Programs use `x`/`y` in different 128-byte lines of one 4 KiB page; mixed-line effects of two nearby variables in one
  line, pair/SIMD/LSE forms other than `swpal`, acquire/release data accesses, and non-write-back memory are not covered.
- Exception entry is EL1 to EL1 (`svc`, `eret`, SP_EL1), not a guest-visible vector delivery with changed translation.
- "Aligned" `swpal` crossing no 16-byte boundary was tested only with writers racing plain loads, not other atomics.
- Page crossing uses two adjacent identity-mapped pages with identical attributes.

## Files

`probe.c` host (VM, four owner threads, cell loop, JSON rows), `guest.S` EL1 boot, vector table, barrier and iteration loop,
`gen.py` generates `cells.S`/`cells.json` from `litmus.py`, `oracle.py` allowed sets and verdict logic (`--self-check`),
`run.py` driver, `entitlements.plist` (hypervisor entitlement, unchanged from the pair-atomicity experiment). The first
author's sources were reused; this pass added the atomicity rounds, `swpal`, split control, mixed sizes, page crossing,
fault handling, oracle, driver and this record.
