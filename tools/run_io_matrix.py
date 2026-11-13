#!/usr/bin/env python3
"""Ordinary installed-root disk use and bounded fio matrix, identical guests on natemu/QEMU.

The guest pins its single virtio IRQ to CPU 0 and runs every fio job on CPUs 1..n-1 (with 2 vCPUs all jobs, including
the four of rand-read-4k-4jobs-q16, share CPU 1; with 1 vCPU nothing is pinned): left free, Linux sometimes put fio on the
IRQ's vCPU and rates fell by half (docs/restructure/io-benchmarks.md).
prepare/self-check never run a VM. run owns one fresh main/read-only-restart pair,
600/120 seconds, no retry. Whole-system measurements, not isolated SSD numbers.
"""
import argparse
import copy
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

import run_fio_admission as admission
import run_fio_baselines as baselines

block = admission.block
digest, write_json, checked = admission.digest, admission.write_json, admission.checked
DISK_BYTES, FILE_BYTES = 2 << 30, 256 << 20
FILES = tuple(f'/bench/data-{i}.bin' for i in range(4))
TIMED = ('seq-read-4k-q16', 'seq-read-128k-q16', 'seq-write-128k-q16',
         'rand-read-4k-q1', 'rand-read-4k-q16', 'rand-read-4k-q64',
         'rand-read-4k-4jobs-q16', 'mixed-4k-q16')
WORKLOADS = TIMED + ('fsync-4k-q1',)
PROFILES = ('precondition',) + WORKLOADS + ('verify-all',)
SOURCES = (Path(__file__), Path(admission.__file__), Path(baselines.__file__), Path(block.__file__), Path(block.__file__).with_name('run_virtio_rng.py'))


def jobs(profile):
    """Explicit per-job options avoid global inheritance ambiguity in fio JSON."""
    count = 4 if profile in ('precondition', 'verify-all', 'rand-read-4k-4jobs-q16') else 1
    result = []
    for i in range(count):
        name = profile + (f'-{i}' if count == 4 else '')
        o = dict(filename=FILES[i], size='256m', rw='read', bs='4k', ioengine='io_uring',
                 iodepth='16', direct='1', numjobs='1', verify='crc32c', verify_interval='4k',
                 verify_fatal='1', randrepeat='1', randseed=str(20261108 + i), allow_file_create='0')
        if profile == 'precondition':
            o.update(rw='write', allow_file_create='1', do_verify='1', end_fsync='1', stonewall='1')
        elif profile == 'verify-all':
            o.update(stonewall='1')
        elif profile == 'fsync-4k-q1':
            o.update(rw='write', iodepth='1', number_ios='64', do_verify='0', fsync='1', end_fsync='1')
        else:
            o.update(time_based='1', runtime='5', do_verify='0')
            if '128k' in profile: o['bs'] = '128k'
            if profile.startswith('seq-write'): o.update(rw='write', end_fsync='1')
            if profile.startswith('rand-read'): o['rw'] = 'randread'
            if profile.endswith('-q1'): o['iodepth'] = '1'
            if profile.endswith('-q64'): o['iodepth'] = '64'
            if profile.startswith('mixed'): o.update(rw='randrw', rwmixread='70', end_fsync='1', serialize_overlap='1')
        result.append((name, o))
    return result


def profile_text(profile):
    return '\n'.join('[' + n + ']\n' + ''.join(f'{k}={v}\n' for k, v in o.items()) for n, o in jobs(profile))


INIT = r'''#!/bin/sh
export PATH=/usr/bin:/bin HOME=/tmp
exec </dev/console >/dev/console 2>&1
export_logs() {
  for f in /tmp/matrix/*; do
    test -f "$f" || continue
    name=$(basename "$f")
    echo "IO-MATRIX-LOG-BEGIN $name"
    cat "$f"
    echo
    echo "IO-MATRIX-LOG-END $name"
  done
}
# BusyBox stty restores attributes with TCSADRAIN, including when unchanged.
drain_console() { console_mode=$(/bin/busybox stty -F /dev/console -g) && /bin/busybox stty -F /dev/console "$console_mode"; }
fail() { echo "IO-MATRIX-FAIL $*"; export_logs; dmesg; drain_console || true; poweroff -f; while :; do sleep 1; done; }
mark() { read up rest < /proc/uptime; echo "IO-MATRIX-MARK name=$1 uptime=$up"; }
mount -t tmpfs tmpfs /tmp || fail tmpfs
mkdir /tmp/matrix || fail logs
phase=
for item in $(cat /proc/cmdline); do case "$item" in io_phase=*) phase=${item#*=};; esac; done
case "$phase" in write) mode=rw;; verify) mode=ro;; *) fail phase;; esac
echo "IO-MATRIX-BEGIN phase=$phase cpus=$(nproc) kernel=$(uname -r)"
rootinfo=$(awk '$2 == "/" {print $1, $3, $4}' /proc/mounts)
case "$rootinfo" in "/dev/vda ext4 $mode,"*) ;; *) fail "root=$rootinfo";; esac
echo "IO-MATRIX-ROOT $rootinfo"
mark mounted-root
fiopin=
irq=$(awk '/virtio/ {sub(":", "", $1); print $1; exit}' /proc/interrupts)
test -n "$irq" || fail virtio-irq
if test "$(nproc)" -gt 1; then
  fiopin="/bin/busybox taskset -c 1-$(($(nproc) - 1))"
  echo 1 > /proc/irq/$irq/smp_affinity || fail irq-affinity
fi
echo "IO-MATRIX-PIN irq=$irq irq_affinity=$(cat /proc/irq/$irq/smp_affinity_list) fio=${fiopin:-none}"
test "$(cat /sys/block/vda/size)" = 4194304 || fail capacity
test "$(cat /sys/block/vda/queue/logical_block_size)" = 512 || fail sector
test "$(basename "$(readlink -f /sys/block/vda/device/driver)")" = virtio_blk || fail driver
echo IO-MATRIX-DEVICE driver=virtio_blk sectors=4194304 logical=512
for f in max_sectors_kb max_segments max_segment_size write_cache; do echo "IO-MATRIX-QUEUE $f=$(cat /sys/block/vda/queue/$f)"; done
sha256sum -c /fio/inputs.sha256 || fail input-hash
sha256sum -c /fixtures/source.sha256 || fail fixture-input
/usr/bin/fio --version >/tmp/matrix/version.txt 2>/tmp/matrix/version.stderr
rc=$?; echo "IO-MATRIX-EXIT version $rc"; test "$rc" = 0 || fail version
for p in /fio/profiles/*.fio; do
  name=$(basename "$p" .fio)
  /usr/bin/fio --parse-only "$p" >"/tmp/matrix/parse-$name.stdout" 2>"/tmp/matrix/parse-$name.stderr"
  rc=$?; echo "IO-MATRIX-PARSE $name $rc"; test "$rc" = 0 || fail "parse-$name"
done
run_job() {
  name=$1
  mark "job-$name-begin"
  $fiopin /usr/bin/fio "/fio/profiles/$name.fio" --output-format=json --output="/tmp/matrix/$name.json" >"/tmp/matrix/$name.stdout" 2>"/tmp/matrix/$name.stderr"
  rc=$?; echo "IO-MATRIX-EXIT $name $rc"; test "$rc" = 0 || fail "$name"
  mark "job-$name-end"
}
if test "$phase" = write; then
  test ! -e /persist/receipt || fail reused-image
  mark filesystem-begin
  mkdir -p /persist/tree || fail mkdir
  dd if=/fixtures/payload.bin of=/persist/tree/staged.bin bs=4096 conv=fsync 2>/tmp/matrix/fs.log || fail create
  dd if=/fixtures/patch.bin of=/persist/tree/staged.bin bs=4096 seek=123 conv=notrunc,fsync 2>>/tmp/matrix/fs.log || fail overwrite
  cp /persist/tree/staged.bin /persist/tree/copied.bin || fail copy
  cmp /persist/tree/staged.bin /persist/tree/copied.bin || fail compare
  mv /persist/tree/copied.bin /persist/final.bin || fail rename
  rm /persist/tree/staged.bin || fail unlink
  printf 'persisted-v1\n' > /persist/receipt || fail receipt
  mark filesystem-end
  run_job precondition
  for name in @WORKLOADS@; do run_job "$name"; done
fi
sha256sum -c /fixtures/expected.sha256 || fail ordinary-persistence
test ! -e /persist/tree/staged.bin || fail unlinked-file-returned
test ! -e /persist/tree/copied.bin || fail renamed-source-returned
test "$(cat /persist/receipt)" = persisted-v1 || fail receipt-readback
echo IO-MATRIX-ORDINARY-PASS
run_job verify-all
for i in 0 1 2 3; do
  f=/bench/data-$i.bin
  n=$(wc -c < "$f"); test "$n" = 268435456 || fail "file-size-$i"
  hash=$(sha256sum "$f"); hash=${hash%% *}
  echo "IO-MATRIX-DATA file=$i bytes=$n sha256=$hash"
done
sync || fail sync
mount -o remount,ro / || fail remount-ro
rootinfo=$(awk '$2 == "/" {print $1, $3, $4}' /proc/mounts)
case "$rootinfo" in "/dev/vda ext4 ro,"*) ;; *) fail final-root;; esac
echo IO-MATRIX-ROOT-READONLY
export_logs
cat /proc/interrupts
dmesg
echo "IO-MATRIX-PASS phase=$phase"
drain_console || fail console-drain
poweroff -f
fail poweroff-returned
'''.replace('@WORKLOADS@', ' '.join(WORKLOADS))


def prepare(args):
    pin, verified = admission.verify_inputs(args)
    args.out.mkdir(parents=True, exist_ok=False)
    root = args.out / 'rootfs'
    shutil.copytree(args.block_inputs / 'rootfs', root, symlinks=True)
    shutil.copytree(args.fio_prep / 'rootfs', root, symlinks=True, dirs_exist_ok=True)
    if digest(root / 'bin/busybox') != pin['rootfs']['bin/busybox']['sha256']: raise ValueError('BusyBox overwritten')
    (root / 'sbin/init').write_text(INIT); (root / 'sbin/init').chmod(0o755)
    (root / 'bench').mkdir(); (root / 'fio/profiles').mkdir(parents=True)
    for name in PROFILES: (root / 'fio/profiles' / (name + '.fio')).write_text(profile_text(name))
    elf = json.loads((args.fio_prep / 'elf-closure.json').read_text())
    hashes = {name: obj['sha256'] for name, obj in elf['objects'].items()}
    hashes.update({'/fio/profiles/' + p.name: digest(p) for p in (root / 'fio/profiles').glob('*.fio')})
    hashes['/sbin/init'] = digest(root / 'sbin/init')
    (root / 'fio/inputs.sha256').write_text(''.join(f'{h}  {n}\n' for n, h in sorted(hashes.items())))
    for name in ('linux', 'bootstrap.cpio', 'mke2fs.conf'): shutil.copyfile(args.block_inputs / name, args.out / name)
    tools = pin['tools']
    for info in tools.values():
        if digest(info['path']) != info['sha256']: raise ValueError('native tool changed')
    image = args.out / 'root.raw'
    with image.open('xb') as f: f.truncate(DISK_BYTES)
    env = dict(os.environ, MKE2FS_CONFIG=str((args.out / 'mke2fs.conf').resolve()))
    checked([tools['mke2fs']['path'], '-F', '-t', 'ext4', '-b', '4096', '-L', 'IOMATRIX',
             '-U', '673af719-2222-4444-8888-12453af21738', '-E', 'root_owner=0:0', '-d', root, image], args.out / 'mke2fs.log', env)
    edits = ''.join(f'set_inode_field /{p.relative_to(root)} {field} 0\n' for p in sorted(root.rglob('*')) for field in ('uid', 'gid'))
    (args.out / 'ownership.debugfs').write_text(edits)
    checked([tools['debugfs']['path'], '-w', '-f', args.out / 'ownership.debugfs', image], args.out / 'ownership.log')
    checked([tools['e2fsck']['path'], '-fn', image], args.out / 'initial-fsck.log')
    checked([tools['debugfs']['path'], '-R', 'stats', image], args.out / 'filesystem-stats.log')
    free = int(re.search(r'^Free blocks:\s+(\d+)', (args.out / 'filesystem-stats.log').read_text(), re.M)[1]) * 4096
    if free < 4 * FILE_BYTES + (128 << 20): raise ValueError('insufficient disk space for corpus and reserve')
    oracle = args.out / 'oracle'; oracle.mkdir()
    for p in SOURCES: shutil.copyfile(p, oracle / p.name)
    write_json(args.out / 'inputs.json', {'purpose': 'ordinary ext4 disk use and bounded fio matrix',
        'verified_inputs': verified, 'block_inputs': str(args.block_inputs.resolve()), 'fio_preparation': str(args.fio_prep.resolve()),
        'disk_bytes': DISK_BYTES, 'file_bytes': FILE_BYTES, 'files': FILES, 'free_bytes': free, 'tools': tools,
        'producer': {p.name: digest(p) for p in SOURCES}, 'rootfs': admission.tree_manifest(root),
        'profiles': {name: dict(jobs(name)) for name in PROFILES},
        'outputs': {name: digest(args.out / name) for name in ('linux', 'bootstrap.cpio', 'root.raw', 'mke2fs.conf', 'rootfs/sbin/init')}})


def fio_result(data, profile):
    errors = []
    def need(ok, why):
        if not ok: errors.append(why)
    try:
        need(data['fio version'] == 'fio-3.39', 'fio version')
        actual, expected = data['jobs'], jobs(profile)
        need(len(actual) == len(expected), 'job count')
        for j, (name, opts) in zip(actual, expected):
            need(j['jobname'] == name and j['error'] == 0, 'job name/error')
            need({k: str(v) for k, v in j['job options'].items()} == opts, 'exact job options')
            r, w = j['read']['io_bytes'], j['write']['io_bytes']
            if profile == 'precondition': need(r == w == FILE_BYTES, 'complete precondition write+CRC read')
            elif profile == 'verify-all': need(r == FILE_BYTES and w == 0, 'full CRC read without writes')
            elif profile == 'fsync-4k-q1':
                need(r == 0 and w == 64 * 4096 and j['write']['total_ios'] == 64, 'finite fsync writes')
                # fio3.39 ends the64-write loop before its next periodic sync; the
                # required end_fsync runs in TD_FSYNCING, excluded from sync stats.
                need(j['sync']['total_ios'] == 63 and j['sync']['lat_ns']['N'] == 63, '63 timed periodic syncs plus required final end_fsync')
            else:
                need(j['job_runtime'] >= 4900, 'five-second timed workload (100ms rounding allowance)')
                if opts['rw'] in ('read', 'randread'): need(r > 0 and w == 0, 'read-only workload')
                elif opts['rw'] == 'write': need(w > 0 and r == 0, 'write-only workload')
                else:
                    need(r > 0 and w > 0, 'mixed read/write workload')
                    need(j['read']['total_ios'] + j['write']['total_ios'] >= 100 and 0.60 <= r / (r + w) <= 0.80, 'observed mixed read fraction60-80% and >=100 I/Os')
            need(bool(j['iodepth_level']), 'achieved depth distribution')
            for direction in ('read', 'write'):
                q = j[direction]
                need(q['short_ios'] == 0 and q['drop_ios'] == 0, 'short/dropped I/O')
                bs = 4096 if opts['bs'] == '4k' else 128 << 10
                need(q['io_bytes'] == q['total_ios'] * bs, 'bytes match completed I/O count and block size')
                if q['io_bytes']:
                    need(all(math.isfinite(q[k]) and q[k] > 0 for k in ('total_ios', 'bw_bytes', 'iops', 'runtime')), 'positive finite I/O metrics')
                    need(q['clat_ns']['N'] > 0 and math.isfinite(q['clat_ns']['mean']) and q['clat_ns']['mean'] >= 0
                         and bool(q['clat_ns']['percentile']) and all(math.isfinite(v) and v >= 0 for v in q['clat_ns']['percentile'].values()), 'finite latency metrics')
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as e: errors.append('malformed fio JSON: ' + str(e))
    return errors


def judge(text, phase, cpus):
    errors = []
    def need(ok, why):
        if not ok: errors.append(why)
    need(re.findall(r'^IO-MATRIX-BEGIN phase=(\S+) cpus=(\d+) kernel=(\S+)$', text, re.M) == [(phase, str(cpus), block.KVER)], 'phase/CPU/kernel')
    mode = 'rw' if phase == 'write' else 'ro'
    need(len(re.findall(r'^IO-MATRIX-ROOT /dev/vda ext4 ' + mode + r',.*$', text, re.M)) == 1, 'installed root mode')
    need(text.count('\nIO-MATRIX-DEVICE driver=virtio_blk sectors=4194304 logical=512\n') == 1, 'disk geometry/driver')
    pin = 'irq_affinity=0 fio=' + ('/bin/busybox taskset -c 1-%d' % (cpus - 1) if cpus > 1 else 'none')
    need(re.findall(r'^IO-MATRIX-PIN irq=\d+ (.*)$', text, re.M) == [pin], 'IRQ/fio pinning')
    need(re.findall(r'^IO-MATRIX-PARSE (\S+) (\d+)$', text, re.M) == [(n, '0') for n in sorted(PROFILES)], 'all profiles parsed exactly once')
    executed = PROFILES if phase == 'write' else ('verify-all',)
    need(re.findall(r'^IO-MATRIX-EXIT (\S+) (\d+)$', text, re.M) == [('version', '0')] + [(n, '0') for n in executed], 'fio execution order/status')
    markers = re.findall(r'^IO-MATRIX-MARK name=(\S+) uptime=([0-9]+\.[0-9]+)$', text, re.M)
    expected_markers = ['mounted-root'] + (['filesystem-begin', 'filesystem-end'] if phase == 'write' else [])
    expected_markers += [f'job-{n}-{point}' for n in executed for point in ('begin', 'end')]
    need([n for n, _ in markers] == expected_markers, 'guest milestone order/count')
    need(all(float(a[1]) <= float(b[1]) for a, b in zip(markers, markers[1:])), 'guest milestone clock order')
    expected = {'version.txt', 'version.stderr'} | {f'parse-{n}.{s}' for n in PROFILES for s in ('stdout', 'stderr')} | {f'{n}.{s}' for n in executed for s in ('json', 'stdout', 'stderr')}
    if phase == 'write': expected.add('fs.log')
    logs = {}
    for name, content, end in re.findall(r'^IO-MATRIX-LOG-BEGIN ([\w.-]+)\n(.*?)^IO-MATRIX-LOG-END ([\w.-]+)$', text, re.M | re.S):
        need(name == end and name not in logs, 'duplicate/mismatched log frame'); logs[name] = content
    need(set(logs) == expected, 'log file set')
    need(len(re.findall(r'^IO-MATRIX-LOG-BEGIN(?: |$)', text, re.M)) == len(expected), 'log begin count')
    need(len(re.findall(r'^IO-MATRIX-LOG-END(?: |$)', text, re.M)) == len(expected), 'log end count')
    need(logs.get('version.txt', '').strip() == 'fio-3.39', 'fio version')
    results = {}
    for name in executed:
        try:
            data = json.loads(logs[name + '.json']); results[name] = data
            errors.extend(name + ': ' + e for e in fio_result(data, name))
        except (KeyError, ValueError, TypeError) as e: errors.append(name + ': missing/malformed JSON ' + str(e))
    hashes = re.findall(r'^IO-MATRIX-DATA file=(\d+) bytes=(\d+) sha256=([a-f0-9]{64})$', text, re.M)
    need([(n, size) for n, size, _ in hashes] == [(str(i), str(FILE_BYTES)) for i in range(4)], 'four complete file hashes')
    need(text.count('\nIO-MATRIX-ORDINARY-PASS\n') == 1, 'ordinary filesystem operations')
    need(text.count('\nIO-MATRIX-ROOT-READONLY\n') == 1, 'read-only shutdown root')
    need(re.findall(r'^IO-MATRIX-PASS phase=(\S+)$', text, re.M) == [phase], 'guest completion')
    need('Power down' in text, 'S5 shutdown')
    need(not re.search(r'IO-MATRIX-FAIL|VIRTIO-BLOCK-FAIL|BUG:|Oops:|Kernel panic|Call Trace:|EXT4-fs error|Buffer I/O error|fio: end_fsync failed', text), 'guest/kernel/filesystem/final-sync error')
    return errors, logs, results, [h for _, _, h in hashes]


def execute(command, case, limit):
    command = ['/usr/bin/time', '-lp'] + command
    write_json(case / 'command.json', command)
    env = dict(os.environ); env.pop('NATEMU_PERF_LOG', None)
    began = time.monotonic(); timeout = limited = False
    markers, offset, pending = [], 0, ''
    def observe():
        nonlocal offset, pending
        serial = case / 'serial.log'
        if serial.exists():
            with serial.open(errors='replace') as f: f.seek(offset); pending += f.read(); offset = f.tell()
            lines = pending.split('\n'); pending = lines.pop()
            for line in lines:
                line = line.replace('\r', '')
                if line.startswith(('IO-MATRIX-BEGIN', 'IO-MATRIX-ROOT ', 'IO-MATRIX-MARK', 'IO-MATRIX-PASS')):
                    markers.append({'host_seconds': time.monotonic() - began, 'line': line})
        return sum(p.stat().st_size for p in case.glob('*.log')) > 32 << 20
    with (case / 'stdout.log').open('wb') as out, (case / 'stderr.log').open('wb') as err:
        p = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, start_new_session=True)
        try:
            while p.poll() is None:
                timeout = time.monotonic() - began >= limit; limited = observe()
                if timeout or limited: block.stop_group(p); break
                try: p.wait(timeout=0.25)
                except subprocess.TimeoutExpired: pass
        except BaseException: block.stop_group(p); raise
    limited |= observe()
    timing = {k: float(v) for k, v in re.findall(r'^(real|user|sys)\s+([0-9.]+)$', (case / 'stderr.log').read_text(errors='replace'), re.M)}
    return {'seconds': time.monotonic() - began, 'returncode': p.returncode, 'timeout': timeout, 'log_limit': limited,
            'host_time': timing, 'markers': markers, 'marker_note': 'host first-observation times, polled at250ms; guest uptime retained separately; not exact boot instruction timing'}


def run(args):
    pin = json.loads((args.inputs / 'inputs.json').read_text())
    for name, h in pin['outputs'].items():
        if digest(args.inputs / name) != h: raise ValueError('input changed: ' + name)
    if pin['profiles'] != {n: dict(jobs(n)) for n in PROFILES}: raise ValueError('profile manifest differs')
    fsck = Path(pin['tools']['e2fsck']['path'])
    if digest(fsck) != pin['tools']['e2fsck']['sha256']: raise ValueError('fsck changed')
    binary = args.executable.resolve(strict=True)
    artifacts = {str(binary): digest(binary)}
    if args.backend == 'natemu':
        for name in ('runtime', 'fexc'):
            p = getattr(args, name)
            if p is None: raise ValueError(name + ' required')
            artifacts[str(p.resolve())] = digest(p)
        if args.build_manifest is None: raise ValueError('natemu build manifest required')
        build = json.loads(args.build_manifest.read_text())
        if any(build['artifacts'].get(p) != h for p, h in artifacts.items()): raise ValueError('build binding differs')
    else:
        version = subprocess.check_output([str(binary), '--version'], text=True, timeout=10)
        if not version.startswith('QEMU emulator version 11.1.2'): raise ValueError('QEMU version differs')
    args.out.mkdir(parents=True, exist_ok=False)
    write_json(args.out / 'manifest.json', {'backend': args.backend, 'cpus': args.cpus, 'artifacts': artifacts,
        'input_manifest': {'path': str(args.inputs / 'inputs.json'), 'sha256': digest(args.inputs / 'inputs.json')},
        'oracle_sources': {str(p.resolve()): digest(p) for p in SOURCES},
        'bounds': {'vm_launches': 2, 'main_seconds': 600, 'verify_seconds': 120, 'log_bytes_per_vm': 32 << 20},
        'differences': ['natemu MMIO/one native active request; QEMU PCI/threads backend', 'natemu F_FULLFSYNC versus QEMU fsync',
                        'CRC32C on reads and writes: checksum-verified whole-system workload rates, not raw device rates',
                        'same guest files, fio jobs and ext4; no host cache purge; whole-system results, not pure disk scores']})
    results, persisted = [], None
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        image = args.out / 'root.raw'
        checked(['cp', '-c', args.inputs / 'root.raw', image], args.out / 'clone.log')
        if digest(image) != pin['outputs']['root.raw']: raise ValueError('clone differs')
        for phase in ('write', 'verify'):
            case = args.out / phase; case.mkdir(); before = digest(image)
            cmdline = f'console=ttyS0 earlyprintk=serial mitigations=off nokaslr root=/dev/vda io_phase={phase} io_rng=0'
            if args.backend == 'natemu':
                command = [str(binary), '--runtime', str(args.runtime), '--fexc', str(args.fexc), '--kernel', str(args.inputs / 'linux'),
                    '--initrd', str(args.inputs / 'bootstrap.cpio'), '--disk', str(image), '--vcpus', str(args.cpus), '--ram', '2G',
                    '--compile-cache', 'off', '--serial', str(case / 'serial.log'), '--cmdline', cmdline]
            else:
                command = [str(binary), '-machine', 'pc-i440fx-11.1', '-accel', 'tcg,thread=multi', '-cpu', 'max', '-smp', str(args.cpus),
                    '-m', '2048', '-nodefaults', '-display', 'none', '-monitor', 'none', '-no-reboot', '-serial', 'file:' + str(case / 'serial.log'),
                    '-kernel', str(args.inputs / 'linux'), '-initrd', str(args.inputs / 'bootstrap.cpio'), '-append', cmdline,
                    '-drive', f'file={image},format=raw,if=none,id=disk,cache=writeback,aio=threads', '-device', baselines.DEVICE]
            result = execute(command, case, 600 if phase == 'write' else 120)
            serial = case / 'serial.log'; text = serial.read_text(errors='replace').replace('\r', '') if serial.exists() else ''
            errors, logs, data, hashes = judge(text, phase, args.cpus)
            exported = case / 'fio'; exported.mkdir()
            for name, content in logs.items(): (exported / name).write_text(content)
            if result['returncode'] != 0 or result['timeout'] or result['log_limit']: errors.append('process failure/timeout/log limit')
            if set(result['host_time']) != {'real', 'user', 'sys'}: errors.append('missing host CPU/time metrics')
            try: checked([fsck, '-fn', image], case / 'fsck.log')
            except (RuntimeError, subprocess.TimeoutExpired) as e: errors.append('offline fsck: ' + str(e))
            after = digest(image)
            if phase == 'write':
                if before == after: errors.append('main image unchanged')
                persisted = hashes
            elif hashes != persisted or before != after: errors.append('persisted hashes or read-only image changed')
            result.update(phase=phase, disk_before=before, disk_after=after, file_sha256=hashes, errors=errors, passed=not errors,
                          fio={n: d['jobs'] for n, d in data.items()})
            write_json(case / 'result.json', result); results.append(result); write_json(args.out / 'results.json', results)
            print(json.dumps({'phase': phase, 'passed': not errors, 'seconds': result['seconds'], 'errors': errors}), flush=True)
            if errors: return 1
        # Both phases passed and the image's hashes and fsck are recorded: the ~1.3 GB per-run copy
        # is no longer evidence (a failing run keeps it for diagnosis).
        image.unlink()
    return 0


def sample(profile):
    result = {'fio version': 'fio-3.39', 'jobs': []}
    for name, o in jobs(profile):
        bs = 4096 if o['bs'] == '4k' else 128 << 10
        r = FILE_BYTES if profile in ('precondition', 'verify-all') else (bs if o['rw'] in ('read', 'randread') else (70 * bs if o['rw'] == 'randrw' else 0))
        w = FILE_BYTES if profile == 'precondition' else (64 * 4096 if profile == 'fsync-4k-q1' else (30 * bs if o['rw'] == 'randrw' else (bs if o['rw'] == 'write' else 0)))
        def metric(n): return {'io_bytes': n, 'total_ios': n // bs, 'bw_bytes': n, 'iops': n // bs, 'runtime': 5000,
                               'short_ios': 0, 'drop_ios': 0, 'clat_ns': {'N': n // bs, 'mean': 1000, 'percentile': {'99.000000': 2000}}}
        result['jobs'].append({'jobname': name, 'error': 0, 'job options': o, 'read': metric(r), 'write': metric(w), 'sync': {'total_ios': 63, 'lat_ns': {'N': 63}},
                               'job_runtime': 5000, 'iodepth_level': {o['iodepth']: 100}})
    return result


def sample_serial(phase, cpus):
    executed = PROFILES if phase == 'write' else ('verify-all',)
    logs = {'version.txt': 'fio-3.39\n', 'version.stderr': ''}
    logs.update({f'parse-{n}.{s}': '' for n in PROFILES for s in ('stdout', 'stderr')})
    for n in executed:
        logs.update({n + '.json': json.dumps(sample(n)), n + '.stdout': '', n + '.stderr': ''})
    if phase == 'write': logs['fs.log'] = 'fixture dd output\n'
    mode = 'rw' if phase == 'write' else 'ro'
    lines = [f'IO-MATRIX-BEGIN phase={phase} cpus={cpus} kernel={block.KVER}',
             f'IO-MATRIX-ROOT /dev/vda ext4 {mode},relatime',
             'IO-MATRIX-PIN irq=16 ' + ('irq_affinity=0 fio=/bin/busybox taskset -c 1-%d' % (cpus - 1) if cpus > 1 else 'irq_affinity=0 fio=none'),
             'IO-MATRIX-DEVICE driver=virtio_blk sectors=4194304 logical=512',
             'IO-MATRIX-MARK name=mounted-root uptime=1.00', 'IO-MATRIX-EXIT version 0']
    lines += [f'IO-MATRIX-PARSE {n} 0' for n in sorted(PROFILES)]
    if phase == 'write': lines += ['IO-MATRIX-MARK name=filesystem-begin uptime=1.00', 'IO-MATRIX-MARK name=filesystem-end uptime=1.00']
    for n in executed:
        lines += [f'IO-MATRIX-MARK name=job-{n}-begin uptime=1.00', f'IO-MATRIX-EXIT {n} 0', f'IO-MATRIX-MARK name=job-{n}-end uptime=1.00']
    lines += ['IO-MATRIX-ORDINARY-PASS'] + [f'IO-MATRIX-DATA file={i} bytes={FILE_BYTES} sha256=' + str(i) * 64 for i in range(4)]
    lines += ['IO-MATRIX-ROOT-READONLY']
    for n, content in sorted(logs.items()): lines += [f'IO-MATRIX-LOG-BEGIN {n}', content, f'IO-MATRIX-LOG-END {n}']
    lines += [f'IO-MATRIX-PASS phase={phase}', 'reboot: Power down']
    return '\n' + '\n'.join(lines) + '\n'


def self_check():
    count = 0
    for profile in PROFILES:
        good = sample(profile); assert not fio_result(good, profile), (profile, fio_result(good, profile))
        for key, value in [('error', 5), ('jobname', 'other'), ('iodepth_level', {})]:
            bad = copy.deepcopy(good); bad['jobs'][0][key] = value; assert fio_result(bad, profile); count += 1
        for key in jobs(profile)[0][1]:
            bad = copy.deepcopy(good); bad['jobs'][0]['job options'][key] = 'wrong'; assert fio_result(bad, profile); count += 1
        bad = copy.deepcopy(good); bad['jobs'].pop(); assert fio_result(bad, profile); count += 1
        direction = 'write' if profile == 'fsync-4k-q1' or profile.startswith('seq-write') else 'read'
        for key, value in [('short_ios', 1), ('drop_ios', 1), ('total_ios', 0), ('bw_bytes', float('nan')), ('iops', 0), ('runtime', 0)]:
            bad = copy.deepcopy(good); bad['jobs'][0][direction][key] = value; assert fio_result(bad, profile); count += 1
        bad = copy.deepcopy(good); bad['jobs'][0][direction]['clat_ns']['mean'] = float('inf'); assert fio_result(bad, profile); count += 1
        if profile in ('precondition', 'verify-all', 'fsync-4k-q1'):
            bad = copy.deepcopy(good); bad['jobs'][0]['write' if profile == 'fsync-4k-q1' else 'read']['io_bytes'] -= 4096
            assert fio_result(bad, profile); count += 1
        if profile == 'fsync-4k-q1':
            for value in (62, 64):
                bad = copy.deepcopy(good); bad['jobs'][0]['sync']['total_ios'] = value; assert fio_result(bad, profile); count += 1
                bad = copy.deepcopy(good); bad['jobs'][0]['sync']['lat_ns']['N'] = value; assert fio_result(bad, profile); count += 1
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / 'init'; p.write_text(INIT); subprocess.run(['/bin/sh', '-n', str(p)], check=True)
    for phase in ('write', 'verify'):
        for cpus in (1, 2, 4):
            text = sample_serial(phase, cpus)
            assert not judge(text, phase, cpus)[0], judge(text, phase, cpus)[0]
            changes = [('cpus=' + str(cpus), 'cpus=99'), ('IO-MATRIX-ORDINARY-PASS', 'missing'),
                       ('IO-MATRIX-ROOT-READONLY', 'missing'), ('IO-MATRIX-PARSE precondition 0', 'IO-MATRIX-PARSE precondition 1'),
                       ('IO-MATRIX-EXIT verify-all 0', 'IO-MATRIX-EXIT verify-all 5'), ('bytes=268435456', 'bytes=1'),
                       ('IO-MATRIX-LOG-END version.txt', 'IO-MATRIX-LOG-END other.txt'), ('Power down', 'not-shutdown'),
                       ('name=mounted-root', 'name=other'), ('IO-MATRIX-PASS phase=', 'IO-MATRIX-FAIL phase=')]
            for a, b in changes:
                assert judge(text.replace(a, b, 1), phase, cpus)[0], (phase, a); count += 1
            assert judge(text + 'IO-MATRIX-LOG-BEGIN version.txt\nfio-3.39\nIO-MATRIX-LOG-END version.txt\n', phase, cpus)[0]; count += 1
            assert judge(text + 'IO-MATRIX-DATA file=0 bytes=268435456 sha256=' + '0' * 64 + '\n', phase, cpus)[0]; count += 1
            assert judge(text.replace('fio-3.39\n', 'fio-3.39\nfio: end_fsync failed for file /bench/data-0.bin\n', 1), phase, cpus)[0]; count += 1
    print(f'{count} negative controls, six valid serial cases and guest shell syntax pass; no VM/fio execution')


def main():
    p = argparse.ArgumentParser(description=__doc__); sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('self-check')
    a = sub.add_parser('prepare')
    for n in ('fio-prep', 'block-inputs', 'out'): a.add_argument('--' + n, type=Path, required=True)
    a = sub.add_parser('run'); a.add_argument('--backend', choices=('natemu', 'qemu'), required=True)
    a.add_argument('--cpus', type=int, choices=(1, 2, 4), required=True)
    for n in ('executable', 'inputs', 'out', 'lock'): a.add_argument('--' + n, type=Path, required=True)
    for n in ('runtime', 'fexc', 'build-manifest'): a.add_argument('--' + n, type=Path)
    args = p.parse_args()
    for k, v in vars(args).items():
        if isinstance(v, Path): setattr(args, k, v.resolve())
    if args.action == 'self-check': self_check(); return 0
    if args.action == 'prepare': prepare(args); return 0
    return run(args)


if __name__ == '__main__': raise SystemExit(main())
