# Correctness pass: stress-ng self-verifying stress runs in a Linux guest

Status: **in progress** (November 6, 2026). Evidence, rerun steps and raw logs: `natemu-raw/correct-stress-20261106-01/`
(README there). Scripts: `tools/correctness/` (this lane's new files `stress-remote.sh`, `run-stress.sh`,
`stress-list.txt`; lane 1's harness reused unchanged: `boot.py`, `vm.sh`, `idle-boot.sh`, `init`, `remote.sh`'s
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

(filled in as each family runs)

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
   without split locks) and the atomic `misaligned` methods run natively only (`splitx`).

## Next steps

(filled in)
