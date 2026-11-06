#!/usr/bin/env python3
"""ltp-list.py LTPROOT LTPSRC OUTDIR: the LTP test list the guest and the native run execute, and what is left out.
OUTDIR/ltp-list.txt: RUNTEST<TAB>NAME<TAB>COMMAND for every entry of the runtest files in RUNTEST (in file order,
an entry whose command another file already listed dropped); OUTDIR/ltp-excluded.txt: RUNTEST<TAB>NAME<TAB>REASON;
OUTDIR/ltp-keep.txt: the testcases/bin files the guest image carries (all but the entry programs of runtest files
not run, and of excluded entries that no kept entry uses)."""
import os, re, sys

RUNTEST = 'syscalls math sched nptl pty mm cve crypto fs fcntl-locktests capability watchqueue'.split()
# Source markers (tst_test fields / headers) that make a test unrunnable or meaningless here.
MARKS = [(r'\.needs_device\b|\.mount_device\b|\.all_filesystems\b|\.format_device\b|\.needs_rofs\b',
          'needs a block (loop) device: none in an initramfs-only machine'),
         (r'tst_timer_test\.h', 'judges wall-clock sleep latency against fixed thresholds (real time limit)')]
# Entries left out by name, each with its reason.
NAMED = {
    **{f'gf{i:02d}': 'growfiles with -L/-T time limits (time-boxed work)' for i in range(1, 31)},
    **{f'rwtest0{i}': 'rwtest -i Ns / iteration counts against time (time-boxed)' for i in range(1, 5)},
    'iogen01': 'rwtest -i 120s (time-boxed)',
    'read_all_dev': 'reads every /dev node (device-dependent: natemu and QEMU expose different devices)',
    'fs_racer': 'fs_racer.sh -t 5: runs for a fixed time (time-boxed)',
    'quota_remount_test01': 'needs a loop device and quota tools', 'isofs': 'needs mkisofs and a loop device',
    'squashfs01': 'needs a loop device', 'fs_fill': 'needs a block device per file system',
    **{f'oom0{i}': 'OOM killer: fills all memory; outcome and time depend on reclaim timing' for i in range(1, 6)},
    **{n: 'KSM: waits on ksmd scan timing (time-boxed polling, minutes per test)' for n in
       'ksm01 ksm01_1 ksm02 ksm02_1 ksm03 ksm03_1 ksm04 ksm04_1 ksm05 ksm06 ksm06_1 ksm06_2 ksm07'.split()},
    'swapping01': 'needs swap', 'thp01': 'thp01 -I 120: iterations bounded by time',
    'mmapstress09': '-t 0.2: runs 0.2 hours (time-boxed)', 'mmapstress10': '-t 0.2: runs 0.2 hours (time-boxed)',
    'mtest06': 'mmap1: runtime-bounded race loop (time-boxed)', 'mtest06_2': 'mmap2 -a: loops until a time limit',
    'mtest06_3': 'mmap3: loops until a time limit', 'min_free_kbytes': 'memory pressure for a fixed duration',
    'mtest01': 'allocates 80% of RAM in parallel: memory-size and reclaim dependent',
    'mtest01w': 'allocates 80% of RAM in parallel: memory-size and reclaim dependent',
    'mallocstress01': 'memory pressure until allocation fails: reclaim dependent',
    'time-schedule01': 'measures scheduling latency (real time limit)',
    'starvation': 'judges scheduler fairness by wall-clock progress (real time limit)',
    'sched_football': 'real-time scheduling referee with a time limit',
    'cfs_bandwidth01': 'judges CFS throttling against wall-clock periods',
}

def main(root, srcdir, out):
    srcs = {}
    for d, _, fs in os.walk(os.path.join(srcdir, 'testcases')):
        for f in fs:
            if f.endswith('.c'): srcs.setdefault(f[:-2], os.path.join(d, f))
    seen, keep_cmds, lst, exc, all_entry = set(), set(), [], [], set()
    rtdir = os.path.join(root, 'runtest')
    for rt in sorted(os.listdir(rtdir)):
        for line in open(os.path.join(rtdir, rt), errors='replace'):
            p = line.split(None, 1)
            if len(p) == 2 and not p[0].startswith('#'): all_entry.add(p[1].split()[0])
    for rt in RUNTEST:
        for line in open(os.path.join(rtdir, rt), errors='replace'):
            line = line.rstrip('\n')
            p = line.split(None, 1)
            if len(p) < 2 or p[0].startswith('#'): continue
            name, cmd = p[0], p[1].strip()
            if cmd in seen: continue
            seen.add(cmd); prog = cmd.split()[0]
            why = NAMED.get(name)
            if not why and prog in srcs:
                text = open(srcs[prog], errors='replace').read()
                for pat, reason in MARKS:
                    if re.search(pat, text): why = reason; break
            if why: exc.append((rt, name, why)); continue
            lst.append((rt, name, cmd)); keep_cmds.add(prog)
    bindir = os.path.join(root, 'testcases', 'bin')
    keep = [f for f in sorted(os.listdir(bindir)) if f in keep_cmds or f not in all_entry]
    open(os.path.join(out, 'ltp-list.txt'), 'w').write(''.join('\t'.join(e) + '\n' for e in lst))
    open(os.path.join(out, 'ltp-excluded.txt'), 'w').write(''.join('\t'.join(e) + '\n' for e in exc))
    open(os.path.join(out, 'ltp-keep.txt'), 'w').write(''.join(f + '\n' for f in keep))
    print(f'listed {len(lst)}, excluded {len(exc)}, bin files kept {len(keep)} of {len(os.listdir(bindir))}')

if __name__ == '__main__':
    main(*sys.argv[1:4])
