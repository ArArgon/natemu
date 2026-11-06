# Correctness pass: stress-ng self-verifying stress runs in a Linux guest

Status: **done** (November 6, 2026; about 7 hours of natemu boots). Evidence, rerun steps and raw logs: `natemu-raw/correct-stress-20261106-01/`
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

Every family ran in natemu at N=4 with seeds 1 and 2 and at N=2 with seed 1 (one boot each, 900 s cap), and natively
with the same parts. Per stressor the instance count is N (part `n=`), the op count fixed (the list); a stressor that
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
| proc1 | fork, vfork, vforkmany, clone, exec, spawn (as nobody), pthread, exit-group, zombie, wait, prctl, ptrace | 3 (+1 as root) | 36 (+12) | 36 (+12; exec/spawn skipped as root) | 0 | clean | 25 min (+6) |
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

The four N=2 boots that reached the 900 s cap (ipc1, code1, split, split2) were not hangs: every stressor that finished
passed, and the one running at the cap was ordinary slow work (ipc1: `shm-sysv`, which spends its 240 s as root doing
no op on natemu and QEMU alike, below; code1: `goto` after `branch` passed; split/split2: misaligned stores, which
natemu runs about 25 times slower than the AMD machine, at their 240 s timeouts one after the other). The work is sized
for 4 instances (fixed total op counts); at N=2 the same ops take about twice as long. Their remaining stressors
(`msg`, `fifo`, `goto`, `misaligned:int32inc`, `int128inc`) passed at N=4 with both seeds; they were not rerun at N=2.

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
447 stressor runs in the 49 (16 families x 3 boots, the mix boot, the root proc1, round 1's capped 16-method split
and splitx; the two round 1 boots that stopped add 12 more passes): 446 passed,
1 skipped (int64wrds, as natively), **0 stress-ng verification errors (`fail:` lines), 0 failed stressors, 0 crashes or
hangs of a stressor**, and in the 42 boots whose kernel log was captured **no kernel oops, BUG, WARNING, general
protection fault, soft lockup, hung task, RCU stall or bad page**: the kernel logs hold only stress-ng's own OOM kills
(163, all of its children: mmapmany, mmapcow, stack and the mix in a 2 GiB guest) and the known MADT checksum line.
Machine stops: 8, all `unsupported_atomic` (finding 1: the 2 round 1 boots, the round 1 split2 boot at its last
stressor, splitx, 4 repro boots). Native: every run passed (but the two environment skips above).

No race between vCPUs, interrupt at an awkward moment, stale code or link, or page-table copy error showed itself: not
in the fork/clone/ptrace page-table copies, the mremap/mprotect/madvise/userfaultfd remaps, TLB shootdowns, code
written and run (icache, far-branch, funccall/funcret), signal delivery, or the many-stressor mixes.

## Next steps

1. Finding 1 has been handed to the no-stop fix (a CPL-3 instruction must not stop the machine; the gather and ICEBP
   findings of lanes 1 and 2 are the same class). After it: boot `repro/splitlock` (`ct=repro part=14` and `62`; expect
   `lock add + xchg ok`), then run `splitx` and the original `mem3` (with `misaligned`) and `cpu1` (with `lockbus`) in
   natemu (remake the image with `x86-overlay.sh` from `stress.cpio.zst` and a list putting them back).
2. Not run, for a later pass: the mix family's other three boots (seeds 2, 3 at N=4, seed 1 at N=2; queue `q8.txt`,
   stopped at wrap-up) and longer mixes; the file-system and file-descriptor stressors (a natemu disk would add the
   device ones); the excluded timing-judged stressors only with tolerances.
3. Slow paths seen (performance, not correctness): misaligned stores about 25x the AMD machine's time, far-branch about
   250x, icache, funccall and prefetch 25-40x.
