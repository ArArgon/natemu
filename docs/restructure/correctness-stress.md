# Correctness pass: stress-ng self-verifying stress runs in a Linux guest

Status: **selected campaign and missing-result follow-up complete; known limitations remain** (November 7 UTC / November 6 local, 2026).
The subsequent [CPU/RAM integration and rerun](correctness-cpu-ram.md) includes the no-stop fixes and completes the
previous integration next step. It also invalidates the original high-count `lockbus --lockbus-nosplit` result as
correctness evidence: stress-ng 0.19.02 walks beyond its allocation at those counts, even when it reports success.
The project selection now uses 500,000 total operations; separate baseline/final controls at N1/N4 pass without
patching the public binary. Original logs and the other campaign observations remain retained.
The original campaign used about 7 hours of natemu boots. Evidence, rerun steps and raw logs: `natemu-raw/correct-stress-20261106-01/`
(README there). Scripts: `tools/correctness/` (this lane's new files `stress-remote.sh`, `run-stress.sh`,
`stress-list.txt`, `stress-check.py` (op counts from calibration; per-stressor comparison with native and the kernel-log
scan), `stress-campaign.sh` (the boot queue, waiting while any `run_suite.py` runs); lane 1's harness reused unchanged: `boot.py`, `vm.sh`, `idle-boot.sh`, `init`, `remote.sh`'s
`sysroot` and `play` stages; lane 2's `x86-campaign.sh`, `x86-overlay.sh`).

Goal: rare concurrency and memory-soundness defects (races between vCPUs, interrupts at awkward moments, stale code or
links, page-table copy errors) under sustained load, judged by stress-ng's own verification.

natemu: the `natemu` stub and release runtime built from 8c48965 in a clean worktree (`build-stub.sh`: stub
`39a31e2d...3f32`, runtime `bb4e8736...b2b2`, the same binaries as lanes 1 and 2), fexc `series65-p2`, compile cache
off, 2 GiB, Debian 6.12.107 kernel, `console=ttyS0 mitigations=off nokaslr`. Reference: the AMD machine
(`ubuntu@192.168.12.11`, Ryzen 7 8845HS KVM guest, 4 vCPUs, 3.9 GB, Ubuntu kernel 7.0.0-42), as its normal user.

## stress-ng

Debian trixie's stress-ng **0.19.02-1** (source package from snapshot `20260915T000000Z`: `.dsc` through apt, checked
against the signed Sources; `.orig.tar.xz` SHA-256 `ffe89071...52bd`, `.debian.tar.xz` `09c5567e...6695`, both equal to
the `.dsc`), built `make STATIC=1` with Debian gcc 14.2.0-19 against Debian libc 2.41 (`stress-remote.sh build`):
one static binary, SHA-256 `53140a75...a7cf`, run unchanged natively and in the guest. Optional libraries Debian links
(libaio, libjpeg, libmpfr, libipsec-mb, ...) are absent from the static build; no chosen stressor needs them.

Image `stress.cpio.zst`: the play rootfs (rebuilt bit-identical, SHA-256 `c3fb9721...7004`) + the correctness `/init` +
`/ct/run-stress.sh`, `/ct/stress-list.txt`, `/ct/stress-ng`.

Runner `run-stress.sh`: per stressor `stress-ng --S N --S-ops OPS --verify --seed SEED --timeout 240 --metrics-brief`
(outer kill at 400 s), its output kept as `CT-LOG` lines, a `CT-RESULT` line with the exit code, the count of
`fail:` lines and the bogo ops done; at the end of each boot the guest's whole kernel log (`CT-DMESG`). OPS is about
5 s of the AMD machine's rate at 4 instances (calibration: `cal=5`, no op limit, 5 s), so every run does a fixed amount
of work.

## Families

The original primary families were attempted in natemu at N=4 with seeds 1 and 2 and at N=2 with seed 1 (one boot
each, 900 s cap), and natively with the same parts; mix and splitx have the exceptions shown below. Per stressor
the instance count is N (part `n=`), the op count fixed (the list); a stressor that
reaches stress-ng's 240 s timeout first stops there (natively too for `mutex`, `mtx`, `pipeherd`, `sem`, `schedmix`,
`yield`, `vforkmany`: their op limit does not stop them).

| Family | Stressors | natemu boots (N4 s1, N4 s2, N2 s1) | stressor runs | pass | fail / verify errors | kernel log | natemu time |
| --- | --- | --- | --- | --- | --- | --- | --- |
| mem1 | vm, vm-rw, memcpy, memrate, stream, mmap, mremap, madvise, mprotect, userfaultfd, msync, msyncmany | 3 | 36 | 36 | 0 | clean | 5 min |
| mem2 | mmapfork, mmapmany, mmapfixed, mmapaddr, munmap, pagemove, remap, rmap, vm-addr, vm-segv, mmapcow, mmaptorture | 3 | 36 | 36 | 0 | 46 stress-ng OOM kills, else clean | 8 min |
| mem3 | bigheap, brk, malloc, stack, stackmmap, mincore, mlock, memfd, ptr-chase, pageswap | 3 | 30 | 30 | 0 | 112 stress-ng OOM kills, else clean | 16 min |
| cpu1 | atomic, futex, mutex, spinmem, dekker, peterson, mtx, prio-inv | 3 | 24 | 24 | 0 | clean | 36 min |
| cpu2 | cpu (all methods), matrix, matrix-3d, vecmath, vecwide, vecfp, vecshuf | 3 | 21 | 21 | 0 | clean | 18 min |
| cpu3 | veccmp, fma, fp, hash, crypt, bitops, intmath, regs, tsc, x86cpuid, rdrand | 3 | 33 | 33 | 0 | clean | 27 min |
| proc1 | fork, vfork, vforkmany, clone, exec, spawn (as nobody), pthread, exit-group, zombie, wait, prctl, ptrace | 3 (+1 as root) | 36 (+12) | 36 (+10 passes, 2 root skips) | 0 | clean | 25 min (+6) |
| proc2 | sigsegv, sigpipe, signal, sigq, kill, sigfpe, sigill, sigtrap, sigbus, sigabrt | 3 | 30 | 30 | 0 | clean | 9 min |
| proc3 | sigchld, sigrt, sigsuspend, sigfd, signest, sigpending, context, bad-altstack, longjmp, priv-instr, usersyscall, x86syscall | 3 | 36 | 36 | 0 | clean | 15 min |
| ipc1 | pipe, pipeherd, eventfd, mq, sem, sem-sysv, shm, shm-sysv, msg, fifo | 3 (N2 capped after 7) | 27 | 27 | 0 | clean (N2: not captured) | 44 min |
| ipc2 | switch, yield, affinity, poll, schedmix, resched, race-sched, sockpair | 3 | 24 | 24 | 0 | clean | 35 min |
| cache1 | cache, cacheline, cachehammer, l1cache, tlb-shootdown, prefetch, flushcache, membarrier, llc-affinity | 3 | 27 | 27 | 0 | clean | 21 min |
| code1 | icache, far-branch, funccall, funcret, branch, goto | 3 (N2 capped after 5) | 17 | 17 | 0 | clean (N2: not captured) | 35 min |
| split | misaligned int16rd/wr/inc, int32rd/wr/wrnt/inc (one method per run) | 3 (N2 capped after 6) | 20 | 20 | 0 | clean (N2: not captured) | 34 min |
| split2 | misaligned int64rd/wr/wrnt/wrds/inc, int128rd/wr/inc | 3 (N4 s1 stopped at lockbus after all 8, N2 capped after 7) | 23 | 22 + 1 skipped (int64wrds: no MOVDIRI, nothing exercised; natively the same) | 0 | clean (N4 s2) | 34 min |
| mix | two runs of 11-12 stressors at once, one instance each, 240 s each (vm, mmapfork, fork, pthread, icache, far-branch, tlb-shootdown, sigsegv, switch, atomic, futex; mprotect, mremap, userfaultfd, vm-rw, clone, signal, sigq, cache, funcret, mmapcow, ptrace, membarrier) | 1 (N4 s1; the other three were not run, wrap-up) | 2 (23 stressor instances) | 2 (all 23 passed) | 0 | 5 stress-ng OOM kills, else clean | 8 min |
| splitx | lockbus (split and nosplit), misaligned all and its 4 atomic methods | native only (natemu: stop, finding 1) | - | - | - | - | - |

The four N=2 boots reached the 900 s cap after 702–889 seconds of completed stressors (ipc1, code1, split, split2).
Every completed stressor passed or recorded an explicit skip; the unfinished stressor has no verdict. The retained
order puts the cap during `shm-sysv`, `goto`, `misaligned:int32inc` and `misaligned:int128inc`, respectively.
Repeated 240-second stressor timeouts explain the cumulative budget pressure; completion elsewhere does not rule
out a hang in an unfinished invocation. The following `msg`/`fifo` also lacked N=2 results. The isolated follow-up
below addresses these tails without discarding the capped observations.

Native (AMD, the same parts): every stressor run passed, but `misaligned:int64wrds` (no MOVDIRI: skipped or nothing
exercised, as in natemu) and `userfaultfd` (skipped as a user). In the calibration runs (no op limit) `shm-sysv` failed
once natively (`shmat ... EINVAL`, instances racing on keys) and `lockbus --lockbus-nosplit` died once of SIGSEGV;
neither recurred with op limits. Every natemu result equals native's (exit code, passed and skipped counts) except:
`userfaultfd` (runs as root in the guest, skipped natively), the capped N=2 tails, and the natemu-only stops of
finding 1.

`shm-sysv` as root (round 1 ipc1): 0 bogo ops in its 240 s on natemu (250 s user, 466 s system CPU) while natively, as
a user, it does its 112 ops in under a second. Probe `runs/x-shmx-n4` / `x-shmx-qemu4` (family `shmx`): as root 0 ops
on natemu **and on QEMU** (same kernel and image), as `nobody` 107 and 108 ops in 0.1-0.2 s on both: stress-ng's root
path on this kernel, not natemu. It still passes (no verification error) and was left in ipc1 as load.

## Exclusions

stress-ng 0.19.02 has 377 stressors (`--verifiable`: 214 always verify, 79 with `--verify`, 84 not at all). Chosen: the
families of `tools/correctness/stress-list.txt` (memory/copy, CPU/atomics/synchronisation, processes/signals,
scheduling/IPC, cache/TLB, code that is written or mapped and run, misaligned accesses), each with `--verify`. Nine
chosen ones have no verification (`madvise`, `memrate`, `mmapcow`, `mmaptorture`, `cache`, `icache`, `tlb-shootdown`,
`flushcache`, `membarrier`): they are judged by exit code, crash, hang and the kernel log
only. Left out, by reason:

- **Devices, hardware or kernel features natemu lacks or the image has none of:** aio, aiol, io-uring (libaio/liburing
  absent from the static build), binderfs, cgroup, cpu-online (CPU offlining: deferred by the owner), dev, efivar, gpu,
  hdd, idle-page, ioport (port 0x80), kvm, led, loop, memhotplug, module, netdev, numa, pci, physmmap, physpage, quota,
  rawdev, reboot, rtc, smi, swap, tun, verity, watchdog, klog, secretmem, pkey, uprobe, ipsec-mb, jpeg, mpfr, judy (libraries absent).
- **Network:** af-alg, dccp, icmp-flood, netlink-proc, netlink-task, ping-sock, rawpkt, rawsock, rawudp, sctp, sock,
  sockabuse, sockdiag, sockfd, sockmany, udp, udp-flood (`sockpair`, AF_UNIX, is in).
- **Timing-judged:** clock, cyclic, hrtimers, itimer, loadavg, min-nanosleep, nanosleep, sleep, timer, timerfd,
  time-warp, softlockup, waitcpu.
- **Fuzzing or deliberately undefined instructions:** opcode, easy-opcode (random opcodes: natemu's refused forms stop
  the machine, a known class, lane 1 finding 2), sysinval, sysbadaddr, enosys, bad-ioctl (no verification either).
- **File-system and file-descriptor stressors** (chattr ... xattr, about 60: dentry, dir*, fallocate, fcntl, flock,
  inotify, link, lock*, open, rename, seek, sendfile, splice, sync-file, utime, ...) and **CPU-only library work**
  (sorts, searches, math, compression, regex, ...: covered in kind by lane 1's torture, glibc math and OpenSSL suites):
  outside this pass's families; candidates for a later one.
- `memthrash`: no verification, and with an op limit its threads run on to the 240 s timeout, natively too (1 GiB at 4
  instances in a 2 GiB guest).
- Moved out of their families after round 1 (finding 1): `misaligned` (all methods), its four atomic methods and
  `lockbus` (with or without split locks), run natively only (`splitx`); in natemu `split` and `split2` run the 15
  non-atomic `misaligned` methods one at a time.

Environment differences (not defects): the guest runs as root, the AMD machine as a user, so `userfaultfd` is skipped
natively (unprivileged userfaultfd off) and runs in the guest; `exec` and `spawn` refuse root, so the guest runs them as
`nobody` (`setpriv`, label `:user`; the first proc1 run skipped them, `runs/s-proc1root-n4-s1`); `misaligned:int64wrds`
(MOVDIR64B) is skipped everywhere. The guest's kernel log holds stress-ng's own OOM kills (its children run with
`oom_score_adj` 1000; mmapmany, mmapcow and stack push into them in a 2 GiB guest): counted, not findings. The kernel's
"Incorrect checksum in table [APIC]" at every boot is the known Linux convention (it zeroes the MADT's wakeup mailbox
address after the checksum; boot-features plan, L2).

## Findings

1. **Known limit (documented typed refusal, owner decision), not a new defect: a LOCKed access crossing a 16-byte
   boundary stops the whole machine** (`Resident { stop: 13 }` = `unsupported_atomic`, boot-handoff §4 "Typed
   refusals"). Round 1 stopped twice: mem3 at `misaligned` (all methods; its atomic methods) and cpu1 at `lockbus`
   (split locks by default); `splitx` (the atomic methods alone) stopped at its first. Minimal repro
   `repro/splitlock/` (`splitlock.c`: one `lock addl` and one `xchg` on 4 bytes at a 64-byte-aligned buffer + OFF;
   image `repro/splitlock.img`, `ct=repro part=OFF`): natively (AMD) OFF 8, 14 and 62 all complete; in natemu OFF 8
   completes and OFF 14 (crossing 16 bytes inside one cache line: not even a split lock on hardware) and 62 (a cache-line
   split) stop the machine, at N=4 and N=1 (`runs/r-splitlock{8,14,62}-n4.*`, `r-splitlock14-n1.*`). The limit is wider
   than hardware's split-lock notion (16 bytes, not 64) and turns one CPL-3 instruction into a whole-machine stop, as
   lane 2's compatibility-mode refusal does; unmodified user programs do this (stress-ng's `misaligned` and `lockbus`
   are ordinary tests; Linux itself only warns on split locks). `lockbus --lockbus-nosplit` stops too (round 1 `split2`, after its
   eight misaligned methods passed): stress-ng's lockbus also does locked increments at buffer+1 and buffer+10 walking
   by 4 bytes whatever the option (`stress-lockbus.c`, `do_misaligned`), so its fourth one (bytes 13-16) crosses 16
   bytes. Coverage kept: `misaligned` one non-atomic method at a time (families `split`, `split2`); `lockbus` (with and
   without split locks) and the atomic `misaligned` methods run natively only (`splitx`). **Handed to the no-stop fix**
   (the lane fixing CPL-3 instructions that stop the machine).

## Totals

natemu: 58 boots, about 7.0 hours of guest time (49 stress boots of record, 6.5 h; the rest repros, probes, the pilot).
447 result entries in the 49 `s-*` boots: **444 passing and 3 skipped** (root `exec` and `spawn`, plus N=2
`int64wrds`). The two `stop-*` round-one boots add 12 passing entries, giving 459 entries: 456 passing and 3 skipped
when included. The former 446-pass/1-skip tally incorrectly counted the two root-only skips as passes.
There are **0 stress-ng verification errors (`fail:` lines) and 0 failed completed stressors**; incomplete stressors
at a cap have no verdict. In the 42 boots whose kernel log was captured there is **no kernel oops, BUG, WARNING, general
protection fault, soft lockup, hung task, RCU stall or bad page**: the kernel logs hold only stress-ng's own OOM kills
(163, all of its children: mmapmany, mmapcow, stack and the mix in a 2 GiB guest) and the known MADT checksum line.
Machine stops: 8, all `unsupported_atomic` (finding 1: the 2 round 1 boots, the round 1 split2 boot at its last
stressor, splitx, 4 repro boots). Native: every run passed (but the two environment skips above).

No race between vCPUs, interrupt at an awkward moment, stale code or link, or page-table copy error showed itself: not
in the fork/clone/ptrace page-table copies, the mremap/mprotect/madvise/userfaultfd remaps, TLB shootdowns, code
written and run (icache, far-branch, funccall/funcret), signal delivery, or the many-stressor mixes.

## Next steps

1. The no-stop state, its compiler dependency and the MOV-SS/ICEBP fix are now integrated and validated in the
   [CPU/RAM follow-up](correctness-cpu-ram.md). Original worktrees remain intact. The blocked-case continuation below
   records the earlier source state; its high-count nosplit result has the later validity correction noted above.
2. The five unfinished `q8` jobs and two isolated N=2 misaligned tails now have results (follow-up below).
   Longer mixes, the excluded file-system/file-descriptor families and timing-judged stressors remain outside this
   completed selection; they are not missing outputs from these seven jobs.
3. Slow paths seen (performance, not correctness): misaligned stores about 25x the AMD machine's time, far-branch about
   250x, icache, funccall and prefetch 25-40x.

## Follow-up of missing and non-passing results (November 7 UTC)

The owner requested execution of planned jobs without outputs and analysis of failed/skipped/capped cases. New raw
data: `natemu-raw/correctness-followup-20261107-01/`. This is a continuation using the original clean 8c48965 producing
snapshot, its retained signed stub/runtime (hashes above), series65-p2 and the same kernel, not a new-build replication.
The deleted stress10 image was reconstructed from the retained base plus `ov/ct/`; that overlay is byte-identical to
the repository's final runner/list. `manifest.json` and `snapshot-sha256.json` retain input and harness identities.
The independent reviewer accepted seven serial development boots with 900-second caps; no timing or general
architecture claim follows. Original outputs are unchanged.

All five missing `q8` jobs completed: mix N=4 seeds 2/3 and N=2 seed 1, IPC tail N=2 seed 1, code tail N=2
seed 1. Two additional isolated tails retain exactly the original options, op counts, seed and instance count:
`misaligned:int32inc` and `misaligned:int128inc` at N=2. Their only list change is the family label used to select
one stressor per boot (`tails/manifest.json`). Every boot reached CT-END and exited zero, without recorded contention,
host cap, unexpected skip, verification failure or kernel-fault diagnostic. `analyze.py` independently checks exact
result IDs/counts, outcome fields and kernel diagnostics; `analysis.json` retains its complete output.

| New boot | Result entries | Passed instances | Guest seconds | stress-ng child OOM kills |
| --- | ---: | ---: | ---: | ---: |
| mix N=4 seed 2 | 2 | 11 + 12 | 484.16 | 4 |
| mix N=4 seed 3 | 2 | 11 + 12 | 484.46 | 4 |
| mix N=2 seed 1 | 2 | 11 + 12 | 484.87 | 2 |
| ipc1t N=2 seed 1 (shm-sysv as nobody, msg, fifo) | 3 | 2 each | 29.85 | 0 |
| code1t N=2 seed 1 (goto) | 1 | 2 | 39.32 | 0 |
| split1t N=2 seed 1 (int32inc) | 1 | 2 | 240.54 | 0 |
| split2t N=2 seed 1 (int128inc) | 1 | 2 | 235.76 | 0 |

Total: **7 boots, 12 passing result entries, 81 passed stressor instances, zero unexpected skips/failures**, 1,998.96
guest seconds. The mixed runs' ten OOM victims are stress-ng's disposable children, marked `oom_score_adj=1000`;
the kernel logs have no other failure diagnostic. Native mix2 has 11 passes and one `userfaultfd` skip; the guest
has 12 passes because it can exercise that root-only facility. Other selected outcome fields match native.

Completion means the bounded stress-ng run finished and verified its work. `int32inc` reached its normal 240-second
stressor timeout at 17,147,157 of 17,250,397 requested operations; it is not a full-operation-target pass. `int128inc`
reached its target (16,916,920 observed, 16,916,919 requested), as did `goto`. Mixed groups likewise retain their
predeclared time/operation stopping rules. These are distinct from the original 900-second host cancellations,
which left no final verdict for the active stressor.

Independent findings review by `/root/rerun_review` checked the retained outcomes and accepted this bounded
follow-up; no new-build replication, general race-freedom or performance claim is made.

The six original host-cap cancellations were the cache/code pilot, the original unsplit misaligned family, and
N=2 ipc1, code1, split and split2. Their completed stressors already consumed 702–889 seconds of the 900-second
boot budget. This supports cumulative budget exhaustion; it does not prove the unfinished stressor completed.
The first two were superseded by smaller families. The four N=2 tails are the focused follow-up above. The eight
`unsupported_atomic` machine stops are distinct from these caps and retain finding 1; this continuation uses the
old build and does not validate the unmerged `no-stop` fix.

Skip interpretation: `exec`/`spawn` refused root and were subsequently exercised as nobody; native `userfaultfd`
is unavailable to the unprivileged reference user but runs in the guest; `int64wrds` reports direct-store support
unavailable or "nothing exercised". Even its exit-zero rows provide no exercised direct-store coverage. Root
`shm-sysv`'s zero-op run is also not functional coverage: the retained nobody/QEMU controls distinguish the environment
path, and the IPC-tail follow-up runs it as nobody. Expected stress-ng child OOM kills remain counted separately
from kernel faults and verification failures.

## Cases blocked by the atomic stop (November 7 UTC)

The owner additionally requested cases skipped after machine stalls or earlier failures. The original `splitx`
N=4, seed-1 boot stopped before its first result; no later no-stop guest run had exercised these seven stress-ng
cases. The continuation in `natemu-raw/correctness-blocked-20261107-01/` runs each in a separate boot with unchanged
operation counts, options and four instances. It uses the retained final **unmerged no-stop** build (`129784c` plus
twelve dirty files), not the old correctness snapshot. Signed stub/runtime/compiler copies, source archive and dirty
patch/files, original compiler build provenance and input hashes are retained. Compiler series69 is selected
explicitly; the older no-stop wrapper default is not used.

All seven boots reach CT-END, with four passed instances each, zero skips/failures, no contention or kernel-fault
diagnostics, and every operation target reached. Their actual operation totals equal the retained native results:

| Case | Requested / completed operations | Guest seconds |
| --- | ---: | ---: |
| lockbus, nosplit | 3,130,083 / 3,130,084 | 86.22 |
| misaligned int16atomic | 63,255 / 63,256 | 52.71 |
| misaligned int32atomic | 63,855 / 63,856 | 51.32 |
| misaligned int64atomic | 64,679 / 64,680 | 51.98 |
| misaligned int128atomic | 62,219 / 62,220 | 50.35 |
| lockbus, split | 124,056 / 124,056 | 35.89 |
| misaligned, all available methods | 14,600 / 14,600 | 57.67 |

The last case exercises eighteen methods; `int64wrds` remains disabled on both native and guest. Independent
preexecution and completed-result reviews accepted these seven bounded observations (28 passed instances).
`analysis.json` preserves exact result identities and diagnostics. These results close the seven previously
unexecuted selections on this no-stop build; they do not establish general split-lock race freedom or change the
old baseline's recorded stops.

Later CPU/RAM diagnosis supersedes acceptance of the `lockbus, nosplit` row above: its pointers overrun the 16 MiB
allocation after 524,287 valid iterations, while this N4 target requests 782,521 per worker. An apparent pass can
therefore overwrite adjacent writable mappings. N1 crashes on baseline/final and QEMU at the same locked-add
instruction after crossing into a protected mapping. See the linked follow-up for fault captures and the separate
allocation-safe controls. The historical row is retained as an observed result, not a valid correctness pass.

The original `mem3` group was also restored, retaining the original order/options and `misaligned` at 14,600
operations. Only the already documented `memthrash` exclusion remains: it was excluded for lacking verification,
not hidden behind a VM stop. All eleven entries pass at N=4, seed 1, with all operation targets reached, in 275.08
guest seconds. Its 45 OOM victims (35 in stack, ten in mlock) are stress-ng's disposable children with
`oom_score_adj=1000`; there is no other kernel-fault diagnostic. The original post-stop `ptr-chase` and `pageswap`
now pass in this restored same-boot context. Independent review accepted the exact selection and results.

The restored `cpu1` group also completes all nine entries at N=4, seed 1, in 797.41 guest seconds: atomic, futex,
mutex, spinmem, lockbus, dekker, peterson, mtx and prio-inv. Every entry reports four passed instances and zero
skips/failures. Lockbus retains the original 123,212-operation limit, not the later splitx control's 124,056.
There is no cap, contention or kernel-fault diagnostic. Peterson verifies 10,355,963 of 51,053,376 requested
operations before its normal 240-second limit (241 seconds including completion). Prio-inv reports 24,812,200 of
25,065,700 after seven seconds; its reported aggregate is also below the configured target. These are successful
bounded stress-ng verifications, not a claim that every configured operation count was attained. Mutex and mtx
also spend their normal 240-second budgets but report counts at or above their targets.

Prio-inv's below-target successful count also appears in retained native controls (17,754,651 and 22,537,664 at
N=4), so this observation is not new or natemu-only. Those unprivileged controls fall back to SCHED_OTHER when FIFO
is unavailable; they do not establish identical scheduling conditions. The pinned 0.19.02-1 source counts work,
signals/reaps children and returns its verification status without asserting final aggregate-count equality. It is
not a timeout, and the precise shortfall mechanism has not been traced. The source URL/hash and control references
are retained in `extra/stress-prio-inv-source.json`.

Across this continuation: **27 passing stress entries, 108 passed instances**, comprising seven isolates and twenty
restored-family entries. The 45 expected OOM victims are confined to mem3. The separate N=4 math and x86-tail results
are recorded in their own reports and must not be folded into these stress totals.
