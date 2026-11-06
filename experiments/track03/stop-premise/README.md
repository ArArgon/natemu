# Stop premise (slice 3, S3b)

Question: does `hv_vm_protect(granule, size, HV_MEMORY_READ)` from another host thread make a *running* vCPU, in a hot loop
with no exits and no MMIO, take a stage-2 write-fault exit by itself (no `hv_vcpus_exit`)? `hv_vm.h` does not say.
Independent of natemu (no FEX, no `crates/`). Raw output: `/Users/liaozping/orca/natemu-raw/stop-premise-20261028-01/`.

## Method

- `probe.c`: 1-4 vCPUs, one thread each (QoS user-interactive), EL1, stage 1 on (4 KiB, one 2 MiB block, Normal WB), EnTSO on.
  Guest loop: `str xzr,[x0]` (the stop store, to the vCPU's own stop granule), `add x2,x2,1`, `str x2,[x1]` (counter in
  another granule, read by the host to prove progress), `b`. One stop granule per vCPU, nothing else in it.
- **IPA granule used: 16 KiB** (`hv_vm_config_get_ipa_granule` on a default config; the default here is 16 KiB). One extra
  run with the granule set to 4 KiB (`probe protect 1 1000 0 4`) also worked: 1000/1000 kicks, p50 1.2 us.
- One kicker thread per vCPU: `t0 = now; hv_vm_protect(granule, G, READ); t1 = now`, then wait for that vCPU's thread to
  report an exit (spin, then `usleep(20)`; the vCPU thread timestamps the exit itself right after `hv_vcpu_run` returns).
  Latency = exit time - t0 (so it includes the protect call). "After return" = exit time - t1, 0 if the exit was seen
  before the call returned. 100 ms after t1 with no exit, the kicker calls `hv_vcpus_exit` to recover and counts it.
- vCPU thread on an exit: checks EC 0x24, WnR, permission fault, IPA inside its own granule, then restores READ|WRITE
  and resumes. `hv_vcpus_exit` is used only in the `exit` mode (protect, then `hv_vcpus_exit(v)`) and for shutdown.
- Modes: `protect` (premise), `exit` (fallback), `control` (no kicks, 5 s; any exit while running is counted).
  1/2/4 vCPUs, N = 10,000 kicks per vCPU (all runs completed N), host with no extra load and oversubscribed
  (12 extra busy-spin threads, QoS default, on an M4 with 4 P + 6 E cores; plus the vCPU and kicker threads: 14-20 threads on 10 logical CPUs).

```
python3 -B experiments/track03/stop-premise/run.py --out /Users/liaozping/orca/natemu-raw/stop-premise-20261028-01 [--kicks 10000]
```
(`build.sh OUT` builds and signs `OUT/probe` with `entitlements.plist`; run `OUT/probe <protect|exit|control> <vcpus> <kicks|secs> [spinners] [granule 4|16]` by hand.)

## Results (M4, macOS 26.6.2, 2026-10-28)

| mode | vCPUs | spinners | kicks (control: exits) | missed 1 ms | missed 100 ms | missed 1 ms after call returned | missed 100 ms after return | latency min/p50/p99/p99.9/max (ns) | protect call p50/p99 (ns) |
|---|---|---|---|---|---|---|---|---|---|
| control | 1 | 0 | 5 s, exits=0 | - | - | - | - | - | - |
| control | 2 | 0 | 5 s, exits=0 | - | - | - | - | - | - |
| control | 4 | 0 | 5 s, exits=0 | - | - | - | - | - | - |
| protect | 1 | 0 | 10000 | 0 | 0 | 0 | 0 | 541/1083/21375/24375/38500 | 750/24333 |
| protect | 2 | 0 | 20000 | 0 | 0 | 0 | 0 | 666/1458/24959/47209/83250 | 1125/26958 |
| protect | 4 | 0 | 40000 | 1 | 0 | 0 | 0 | 1375/3625/203000/379916/2972834 | 4791/212334 |
| exit | 1 | 0 | 10000 | 0 | 0 | 0 | 0 | 1125/1250/21125/24667/29333 | 750/23125 |
| exit | 2 | 0 | 20000 | 0 | 0 | 0 | 0 | 1333/1666/24000/40458/49000 | 1125/25917 |
| exit | 4 | 0 | 40000 | 0 | 0 | 0 | 0 | 1542/3875/115166/256541/946041 | 9125/124875 |
| control | 1 | 12 | 5 s, exits=0 | - | - | - | - | - | - |
| control | 2 | 12 | 5 s, exits=0 | - | - | - | - | - | - |
| control | 4 | 12 | 5 s, exits=0 | - | - | - | - | - | - |
| protect | 1 | 12 | 10000 | 3 | 0 | 0 | 0 | 583/1292/22333/27417/4733083 | 875/23167 |
| protect | 2 | 12 | 20000 | 41 | 0 | 0 | 0 | 666/1500/31333/1916834/36818833 | 1166/37541 |
| protect | 4 | 12 | 40000 | 652 | 0 | 0 | 0 | 666/3375/1719083/7578791/23115250 | 3417/1900708 |
| exit | 1 | 12 | 10000 | 5 | 0 | 5 | 0 | 1166/2084/24375/31625/3194750 | 1792/23792 |
| exit | 2 | 12 | 20000 | 10 | 0 | 0 | 0 | 1333/1625/26000/62500/7066292 | 1166/33208 |
| exit | 4 | 12 | 40000 | 272 | 0 | 3 | 0 | 1417/3917/643500/7095584/25734708 | 7583/965709 |

"kicks" is the total over all vCPUs (N x vCPUs). Syndrome seen on every stage-2 exit, all modes: ESR `0x93df804f` =
EC 0x24 (data abort from a lower EL), ISV=1, SAS=64-bit, SRT=31 (xzr), WnR=1, DFSC 0x0f (permission fault, level 3);
FAR = IPA = the vCPU's stop granule (e.g. `0x40100000` for vCPU 0, +G per vCPU). Every kick produced exactly one such
exit; no other exit reason, EC or address was ever seen; no unexpected faults; counters advanced in every run.
The no-kick controls (1, 2, 4 vCPUs, unloaded and oversubscribed, 5 s each) had zero exits.

## Reading

- `hv_vm_protect` alone does take effect on a running vCPU: 0 of 150,000 kicks were lost, none took 100 ms, none needed `hv_vcpus_exit`.
  Unloaded: 0 misses over 1 ms in 1 and 2 vCPUs, 1 in 40,000 at 4 vCPUs (3.0 ms, inside the protect call; the
  first, unrecorded repeat of the matrix had 0). p50 is 1.1 us (1 vCPU) to 3.6 us (4).
- In `protect` mode the exit is normally seen before `hv_vm_protect` returns (after-return p50 = 0 at 4 vCPUs, ~0.4 us at 1): the call behaves as
  synchronous with respect to running vCPUs, and its own cost (p50 0.75-4.8 us, p99 24-212 us) is most of the latency.
  The ms-scale tail under oversubscription is `hv_vm_protect` blocking (host scheduling): after the call returned,
  `protect` never missed 1 ms in this run (max 164 us); an independent replication (Oct 28) saw up to ~10 ms after the
  call returned when oversubscribed, so part of the tail is the vCPU thread waking late. About 2% of kicks landed while
  the vCPU was still in the host (the random gap can be shorter than re-entry); such kicks fire on re-entry. Concurrent
  protect calls contend (p99 ~200 us with 8 callers, ~30 us with one). `exit` mode shows the same oversubscribed tail (it missed 1 ms after the call returned 8 times), so the tail is
  host scheduling, not an HVF difference; `exit` costs one extra exit per kick.
