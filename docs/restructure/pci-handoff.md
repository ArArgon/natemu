# Virtio over PCI: handoff

State on November 13, 2026: branch `pci` (pushed to `origin/pci`; from `main` at dcffe4b; not merged). The transport is
built, reviewed and boots Debian's kernel. What remains is measurement, the full suite and the records
([pci-plan.md](pci-plan.md) Task 8). This file is what someone needs to finish it on any machine.

## What exists

`natemu --virtio-transport mmio|pci` (default `mmio`); experimental per-device form `disk=pci,net=mmio,rng=mmio`. With
`pci` the guest sees a PCI bus (host bridge 00:00.0, block 00:01.0, network 00:02.0, entropy 00:03.0) and the virtio
devices use MSI-X, or the legacy INTx line (GSI 16/17/18) for a guest that does not enable MSI-X. Design, guest-visible
values and their sources: [pci-plan.md](pci-plan.md) "Design". Stated leniencies and deviations:
`crates/host/src/virtio/README.md` "PCI transport".

| Piece | Where |
| --- | --- |
| MSI delivery to the LAPICs | `crates/host/src/irq.rs` `Irqs::msi` |
| Config space, MSI-X (adapted from cloud-hypervisor f84dae5; divergences listed in each file's header) | `crates/host/src/pci/configuration.rs`, `msix.rs`; licences in `third_party/cloud-hypervisor/` |
| Bus, 0xCF8/0xCFC ports, host bridge, one-range memory window routed by BAR | `crates/host/src/pci/mod.rs` |
| Virtio-PCI function (BAR0 front end, MSI-X/INTx line logic) | `crates/host/src/virtio/pci.rs`, raise path in `virtio/mod.rs` |
| DSDT `PCI0` with `_CRS`/`_PRT`, E820 window, launch option, stderr logger | `crates/host/src/boot/acpi.rs`, `boot.rs`, `linux.rs`, `crates/cli/src/main.rs` |
| Runners' `--transport {mmio,pci}`, guest probe and judge, suite steps `net-linux-pci`, `io-coverage-pci` | `tools/run_net_linux.py`, `run_virtio_block.py`, `run_io_coverage.py`, `run_io_matrix.py`, `io_common.py`, `run_suite.py` |

New dependencies: `thiserror` =2.0.21, `log` =0.4.35 (kept so the adapted files stay close to upstream).

## Evidence so far

- Host crate unit tests: 338 pass, 2 ignored (257 before the work).
  `./tools/with-env cargo test --offline --locked -p natemu --lib`.
- Default launch unchanged: ACPI tables and E820 map pinned by digests recorded before the work
  (`boot::acpi` test `default_tables_and_map_are_unchanged`); the runners' default-mode guest scripts are byte-identical
  to before.
- Acceptance on VMs (Debian 6.12.107): network runner 15/15 at 1/2/4 vCPUs, block runner 6/6, I/O coverage over PCI
  passing apart from `host-full`, which fails identically on MMIO and on `main`; a network vector moved to another CPU
  under traffic; `pci=nomsi` on IO-APIC 16-18; one mixed launch. Final binary (code at 275471b) re-booted: PCI at 2 and
  4 vCPUs, `pci=nomsi`, mixed, MMIO control, all clean.
- The purpose of the work, same boot-and-disk workload at 2 vCPUs: MMIO 409 EOI exits, 409 status reads, 409
  acknowledges, 2,476 device exits; PCI 0, 0, 0 and 1,772 (kicks and setup only).
- Reviews: every task by an independent reviewer; one whole-branch review ("ready after fixes", fixed and re-reviewed).

Not yet evidenced:

- any performance number, and the plan's two performance-flavoured gates: the exit count on a depth-16 fio run (it was
  taken on the boot-and-disk workload instead) and "depth-1 I/O at 1 vCPU at the MMIO rate or better" (a quick `dd`
  comparison was inconclusive);
- the full suite on this branch;
- the network, coverage and matrix runners' PCI mode on VMs after their last two commits (4eec359 and the fix after
  it). The suite's steps `net-linux-pci` and `io-coverage-pci` cover the network and coverage runners;
  `run_io_matrix.py` is exercised by the benchmark below. `run_virtio_block.py` is not a suite step: 4eec359 broke its
  `run` (a variable used before assignment, in both modes); after the fix it passes write and verify at 2 vCPUs in
  PCI and in MMIO mode (`pci-accept-20261113-01/final-block-{pci,mmio}-n2`), 1 and 4 vCPUs not rerun;
- a guest other than Linux.

Raw evidence, outside the repository (this machine's `natemu-raw/`): `pci-smoke-20261113-01/` (the hand boots and the
exit-count logs), `pci-accept-20261113-01/` (the acceptance gates), `pci-bench-20261113-01/` (prepared matrix inputs for
both transports, a `run.sh`, and four runs marked NOT A RESULT because the machine was shared: do not quote them).

## Remaining work

**Status, November 14 evening (branch `pci-rebase`: `pci` rebased onto `main` 1c43377, M4, `natemu-raw` here):**
- Full suite on the rebased tip 9816283 (`natemu-raw/pci-rebase-suite-20261114-01`): 170/173. `io-coverage` and
  `io-coverage-pci` fail only the known `host-full` case. `net-linux-pci` failed because the suite runs it beside
  `net-linux` and both bind the same host ports; fixed in 42e8fcd (a lock on the ports), and both then pass together.
- Fixes for findings of two independent reviews: a lone request's inline disk call no longer runs while an interrupt is
  held (42e8fcd, with a unit test that fails without it); and the halt-poll window now judges a halt by its whole length
  once it waits a second time (it did so only by the first wait, so an idle vCPU woken by doorbells that brought nothing
  kept polling). After both: quick suite 83/85, only `host-full` failing (`pci-rebase-quick-20261114-01`).
- CPU benchmarks, main (4c89205) against the branch's default launch, 3 interleaved rounds, 132 boots, none contended
  (`natemu-raw/pci-rebase-ab-20261114-01/tables.txt`): every benchmark within -1.6..+2.1% at both 1 and 4 vCPUs, except
  4-vCPU locks -5.8% (noise, as in decision 41 of io5-perf-notes) and 4-vCPU hackbench +3.8%. Ten more runs of
  hackbench per arm (`hb/`): means 3.52 s main, 3.62 branch, 3.57 with polling off, 3.57 with the fix. The spreads are
  about 0.6 s, so this is at most a ~3% halt-polling cost and is not resolved. The default launch is not byte for byte
  `main`'s: the disk's 256-entry queue, seg_max 126 and halt polling apply to MMIO too.
- Final commit 0dfd58f: full suite 171/173, only `host-full` on both transports (`pci-rebase-suite-20261114-02`).
- PCI rechecks on 0dfd58f (`natemu-raw/pci-recheck-20261114-01`, binaries pinned in `bin/`): boots at 1/2/4 vCPUs (four
  functions, MSI-X counting, no complaint lines, data and network checks), `pci=nomsi` at 2 and 4 (IO-APIC lines), the
  mixed launch, an MMIO control, a network vector moved to CPUs 2 and 3 under traffic (counts moved, traffic went on),
  and the block runner over PCI at 1/2/4 vCPUs all pass. Depth-1 4 KiB direct `dd` at 1 vCPU, two boots each (warm runs):
  reads 0.096-0.102 s PCI against 0.115-0.118 s MMIO, writes 0.100-0.101 against 0.119-0.122.
- Exit-count gate, the whole fio matrix at 2 vCPUs with the perf log (`gatec-*`): PCI 0 EOI exits on both vCPUs and
  10,697 device exits for 9,648 queue notifications (the rest is setup); MMIO 182,655 EOI exits, 182,818 status reads
  and acknowledges, 611,235 device exits. The notification count is per device, not per vCPU.
- Disk matrix on the M4 (`matrix-table.txt`, kIOPS; one PCI run, two MMIO runs per cell): at 1 vCPU PCI is ahead on
  every queued job (rand-read 4k q16 106 against 42-49) and level on fsync; at 4 vCPUs ahead on 4 KiB jobs (q16 121
  against 91-93, four jobs 283 against 239) and level on 128 KiB (reads 33.5 against 35-36, writes 20.2 against
  16-21).
- IO5 on 0dfd58f (`io5-pci-rebase-20261114-01`, MMIO, mitigations off): PASS, use 533.4 s against main's 522.2 s,
  make 354.3 against 341.3 (+3.8%) with configure and pytest level. Not a regression: make shares the vCPUs with the
  download and generate loops, whose work differed (generate 12 rounds against 10). A same-session pair with
  `--solo-build` (`io5-main-solo-20261114-01`, `io5-branch-solo-20261114-01`, both PASS): make alone 121.4 s on both;
  the combined make 341.1 against 347.4 with 86 against 91 download rounds.
- Network, iperf3 at 2 vCPUs, the November 12 method (`pci-recheck-20261114-01/net`, 3 rounds rotated, Mbit/s):
  main MMIO up 3386-3527, down 5756-5820; branch MMIO 3263-3553, 5726-5882 (level with main); branch PCI 3075-3104,
  5431-5505: single streams over PCI about 9% (up) and 6% (down) behind MMIO on the M4, 8 streams down about 8% ahead.
  Level on the M2 Max; not investigated. Bears on the default-transport decision, not a regression.


**Status, November 14 (branch `halt-poll`, on `pci`; an M2 Max without the M4's `natemu-raw`):** items 2 to 4 are
done in substance, with a session harness instead of the pinned runners
([io-benchmarks.md](io-benchmarks.md), "PCI transport with multiqueue, interrupt hold and a 256-entry ring"):
PCI against MMIO for disk and network, and the records. The branch also adds, for the disk over PCI, one request
queue per vCPU, an interrupt hold and a 256-entry ring, and halt polling for both transports; every matrix row and
every iperf3 measure is at or above the `pci` tip. The suite's I/O steps pass on the branch with inputs rebuilt
here (`io-coverage` and `io-coverage-pci` apart from `host-full`, `net-linux`, `net-linux-pci`, both lifecycle
steps, and the block runner on both transports). Still open: the rest of item 1 (the CPU steps need QEMU reference
corpora from the M4; the quick suite here passes 51 steps, the rest fail identically on the `pci` tip or for those
inputs), the same benchmarks in the pinned form on the M4, and item 5.

1. **Full suite, once**: `python3 tools/run_suite.py --out DIR` on the branch tip; expect the known `io-coverage`
   `host-full` failure only (and the same in `io-coverage-pci`).
2. **Disk benchmark, PCI against MMIO on one binary** (existing matrix runner; QEMU and VZ numbers are reused from
   earlier rounds, not re-measured). On a quiet machine:

   ```sh
   # A = a new raw-results directory; B = the built, signed natemu; R = the resident runtime; F = fexc
   # (docs/restructure/boot-handoff.md has the build, signing and fexc steps). Export all four.
   export A B R F
   python3 - <<'EOF'   # A/build.json: the runner requires a manifest of the binaries by SHA-256, keyed by resolved path
   import hashlib, json, os, pathlib
   a = pathlib.Path(os.environ['A'])
   paths = [pathlib.Path(os.environ[k]).resolve() for k in ('B', 'R', 'F')]
   (a / 'build.json').write_text(json.dumps({'purpose': 'I/O matrix A/B, virtio PCI vs MMIO',
       'artifacts': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}))
   EOF
   for t in mmio pci; do   # fresh inputs per transport: the guest init lives in the image
     python3 tools/run_io_matrix.py prepare --fio-prep FIO_PREP --block-inputs BLOCK_INPUTS --out $A/matrix-$t --transport $t
   done
   for r in 1 2; do for n in 1 4; do
     if [ $((r % 2)) = 1 ]; then order="mmio pci"; else order="pci mmio"; fi   # alternate the order
     for t in $order; do
       python3 tools/run_io_matrix.py run --backend natemu --transport $t --cpus $n --executable $B --inputs $A/matrix-$t \
         --out $A/$t-n$n-r$r --lock $NATEMU_RAW_ROOT/.vm.lock --runtime $R --fexc $F --build-manifest $A/build.json
     done
   done; done
   ```

   `FIO_PREP` and `BLOCK_INPUTS` are the pinned fio preparation and block inputs (here
   `natemu-raw/io-benchmark-prep-20261108-01` and `natemu-raw/io-inputs-20261113-01/block`; elsewhere prepare them as
   [io-benchmarks.md](io-benchmarks.md) describes). The runner takes the VM lock exclusively and does not wait: a run
   that finds it held fails at once and is simply rerun. Each run is about 2 minutes of VM time plus checks.
   Read with: under PCI the disk's interrupt CPU is chosen by Linux (the vector is kernel-managed and refuses
   pinning); the runner records it and keeps fio off it, where MMIO runs pin the interrupt to CPU 0. At 2 vCPUs that
   puts fio on CPU 0 under PCI and CPU 1 under MMIO.
   Expectation to test (an estimate from three exits at about 1.5 us each, not a result): roughly 10-15% on 1-vCPU and
   depth-1 jobs, about level at 4 vCPUs where the fio vCPU is the limit. An earlier quick depth-1 `dd` comparison was
   inconclusive (order effect as large as the difference).
3. **Network benchmark**: the November 12 iperf3 round (2 vCPUs: upload, download, 8 streams each way), PCI against
   MMIO; method in `natemu-raw/net-slirp-20261112-01/vmm-compare-01/README.md`.
4. **Records**: add the measured numbers to `crates/host/src/virtio/README.md` "PCI transport" and to
   [io-benchmarks.md](io-benchmarks.md); update the virtio backlog table in
   `docs/research/investigations/INV-03-01.md` (the "Interrupt path: 3 exits per interrupt" row: exits gone under PCI,
   interrupt count unchanged; the "virtio-pci + MSI-X" row: built, single queue, no multiqueue).
   Also, only once `tools/run_io5.py` has reached `main` (it is on `perf-real-workload`): its `EXPECTED` list of
   guest log lines has two PCI complaints that are MMIO-mode only.
5. **Owner decisions**: the default transport (still `mmio`); the merge; licence housekeeping (the repository has no
   licence file; adapted files keep upstream's headers).

## Things to know when picking this up

- On a fresh machine the builds are `--offline --locked`: fetch the crates once with the network
  (`cargo +1.97.0 fetch --locked`), which now includes `thiserror`, `thiserror-impl` and `log`.
- `natemu-raw/` is not in the repository. Besides the fio preparation and block inputs, the runners' prepared guest
  inputs (kernel, initramfs pieces, the network method's README) live there: copy the directory or regenerate with each
  runner's `prepare`.

- In a git worktree the tools' default raw-results root resolves to the wrong directory: export
  `NATEMU_RAW_ROOT=<the real natemu-raw>` for every runner and for `tools/with-env`.
- `run_virtio_block.py` and `run_io_matrix.py` in PCI mode need inputs prepared with `--transport pci` and refuse
  older ones.
- The disk device's interrupt hold-back and EVENT_IDX are separate backlog items and are not part of this branch:
  PCI removes the exits per interrupt, not the number of interrupts.
- Not built, by design: ECAM/MCFG and PCIe, 64-bit BARs, hotplug, non-virtio PCI devices, the PCI_CFG capability.

## Decisions made while building (each can be reversed)

| Decision | Why | Cost if wrong |
| --- | --- | --- |
| Host bridge presents QEMU `pc`'s identity (8086:1237) | FreeBSD enables MSI in a VM only for bridges on its list; fits the rest of the chipset | one constant |
| A set MSI redirection hint with fixed delivery is sent lowest-priority | SDM 11.11.1 and KVM; QEMU's emulated APIC ignores the hint | one line; no current Linux sets it |
| MSI-X Enable resets to 0; byte writes reach Message Control; pending vectors are released only when "enabled and not masked" becomes true | PCI 3.0 6.8.2; upstream differs in all three | three small divergences from upstream to carry |
| With Bus Master clear, interrupts are held pending and sent when it is set (first ruling was to drop them) | this device processes requests regardless of Bus Master, so a late Bus Master must not lose a completion | differs from QEMU, which blocks DMA and message alike |
| A device reset clears MSI-X pending bits | no interrupt of a closed incarnation (QEMU does the same) | none known |
| BAR0's decode follows the live register, so byte and word writes move it | PCI allows narrow config writes; upstream's cached address ignores them | upstream's `get_bar_addr` cache is stale after narrow writes (unused here) |
| Under PCI, a queue-layout write while the queue is enabled is ignored, not refused | a refusal stops the vCPU | a driver bug is silent instead of fatal |
| Runners' default-mode guest scripts stay byte-identical; PCI text is patched in only in PCI mode | "keep the current MMIO mode" | a `prepare --transport pci` variant to maintain |
| The exit-count check was done on the play image's boot-and-disk workload, not a depth-16 fio run | the bespoke November 11 tooling was not rerun | redo with fio if a per-request figure is wanted |

Deferred, judged acceptable by the whole-branch review: upstream's `vector_ctl` keeps reserved bits; the command
register accepts reserved and I/O-space bits; a 4-byte access at port 0xCFA reads all-ones; `_BBN` is omitted (as QEMU);
no unit test shows an MSI waking a halted vCPU (the VM gates cover it); no second guest OS was booted.
