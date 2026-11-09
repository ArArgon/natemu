#!/usr/bin/env python3
"""N4 verified disk + CPU/memory soak, then a clean read-only restart.

One invocation owns two VM launches (900/120s), no retry/build/download. prepare
uses a new512MiB image and128MiB corpus; this differs from the performance matrix.
"""
import argparse
import base64
import gzip
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import zlib

import run_io_matrix as matrix
admission, block = matrix.admission, matrix.block
digest, checked, write_json = matrix.digest, matrix.checked, matrix.write_json
DISK_BYTES, FILE_BYTES = 512 << 20, 64 << 20
STRESS_SHA = '8de2f3cf5a0c0856b59e54fed9f035fac6ec752820ad6d5df8bd2dc5440814e0'
SOURCE_SHA = 'ffe8907154cc3b65cc66cde8663106355f88c948b58169cee1badbde9e0f52bd'
PROFILES = ('precondition0', 'unloaded', 'verify-before', 'precondition1', 'loaded', 'verify-all')
WORKERS = ('cpu0', 'cpu1', 'vm0')
MAX_LOG_BYTES, MAX_TOTAL_LOG_BYTES = 8 << 20, 16 << 20
STRESS_ARGS = {
    'cpu0': '--cpu 1 --cpu-method int64 --seed 20261108',
    'cpu1': '--cpu 1 --cpu-method int64 --seed 20261109',
    'vm0': '--vm 1 --vm-bytes 268435456 --vm-method one-zero --vm-keep --vm-madvise normal --seed 20261110',
}
SOURCES = (Path(__file__),) + matrix.SOURCES


def jobs(profile):
    result = []
    for i in range(2):
        opts = dict(filename=f'/bench/data-{i}.bin', size='64m', rw='read', bs='4k', ioengine='io_uring',
            iodepth='16', direct='1', numjobs='1', verify='crc32c', verify_interval='4k', verify_fatal='1',
            randrepeat='1', randseed=str(20261108 + i), allow_file_create='0', percentile_list='50:95:99')
        if profile.startswith('precondition'):
            opts.update(rw='write', allow_file_create='1', do_verify='1', end_fsync='1', stonewall='1')
        elif profile.startswith('verify'):
            opts.update(stonewall='1')
        else:
            opts.update(rw='randread' if i == 0 else 'randwrite', time_based='1', runtime='300' if profile == 'loaded' else '60',
                do_verify='0', serialize_overlap='1', log_avg_msec='1000', log_hist_msec='1000',
                write_bw_log=f'/tmp/soak/{profile}-{i}', write_iops_log=f'/tmp/soak/{profile}-{i}',
                write_hist_log=f'/tmp/soak/{profile}-{i}')
            if i == 1: opts['end_fsync'] = '1'
        result.append((f'{profile}-{i}', opts))
    return result


INIT = r'''#!/bin/sh
export PATH=/usr/bin:/bin HOME=/tmp
exec </dev/console >/dev/console 2>&1
pids= monitor_pid=
remove_pid() {
  remaining=
  for q in $pids; do test "$q" = "$1" || remaining="$remaining $q"; done
  pids=$remaining
}
cleanup() {
  if test -n "$monitor_pid"; then kill "$monitor_pid" 2>/dev/null; wait "$monitor_pid" 2>/dev/null; monitor_pid=; fi
  for p in $pids; do /bin/busybox kill -TERM -- -"$p" 2>/dev/null; done
  test -z "$pids" || sleep 1
  for p in $pids; do /bin/busybox kill -KILL -- -"$p" 2>/dev/null; wait "$p" 2>/dev/null; done
  pids=
}
export_logs() {
  for f in /tmp/soak/*; do
    test -f "$f" || continue
    name=$(basename "$f"); bytes=$(wc -c < "$f")
    test "$bytes" -le 8388608 || return 1
    hash=$(sha256sum "$f"); hash=${hash%% *}
    /bin/busybox gzip -1 -c "$f" > /tmp/soak-export.gz || return 1
    /bin/busybox base64 -w 76 /tmp/soak-export.gz > /tmp/soak-export.b64 || return 1
    echo "IO-SOAK-LOG-BEGIN v2 $name bytes=$bytes sha256=$hash"
    # Bound UART bursts after the workload without dropping interval evidence.
    lines=0
    while IFS= read -r line; do
      echo "$line"; lines=$((lines+1))
      if test "$lines" = 8; then sleep 0.05; lines=0; fi
    done < /tmp/soak-export.b64
    echo "IO-SOAK-LOG-END $name"
  done
}
# BusyBox stty restores attributes with TCSADRAIN, including when unchanged.
drain_console() { console_mode=$(/bin/busybox stty -F /dev/console -g) && /bin/busybox stty -F /dev/console "$console_mode"; }
fail() { echo "IO-SOAK-FAIL $*"; cleanup; export_logs || echo IO-SOAK-FAIL-export; dmesg; drain_console || true; poweroff -f; while :; do sleep 1; done; }
trap 'fail signal' TERM INT HUP
mark() { read up rest < /proc/uptime; echo "IO-MATRIX-MARK name=$1 uptime=$up"; }
mount -t tmpfs tmpfs /tmp || fail tmpfs
mkdir /tmp/soak || fail logs
phase=
for item in $(cat /proc/cmdline); do case "$item" in io_phase=*) phase=${item#*=};; esac; done
case "$phase" in write) mode=rw;; verify) mode=ro;; *) fail phase;; esac
echo "IO-SOAK-BEGIN phase=$phase cpus=$(nproc) kernel=$(uname -r)"
rootinfo=$(awk '$2 == "/" {print $1, $3, $4}' /proc/mounts)
case "$rootinfo" in "/dev/vda ext4 $mode,"*) ;; *) fail root;; esac
echo "IO-SOAK-ROOT $rootinfo"
test "$(cat /sys/block/vda/size)" = 1048576 || fail capacity
test "$(basename "$(readlink -f /sys/block/vda/device/driver)")" = virtio_blk || fail driver
sha256sum -c /fio/inputs.sha256 || fail inputs
/usr/bin/fio --version >/tmp/soak/fio-version.txt 2>&1 || fail fio-version
/usr/bin/stress-ng --version >/tmp/soak/stress-version.txt 2>&1 || fail stress-version
/usr/bin/stress-ng --help >/tmp/soak/stress-help.txt 2>&1 || fail stress-help
for applet in setsid gzip base64; do
  /bin/busybox --list | grep -x "$applet" >/dev/null || fail "$applet"
done
for p in /fio/profiles/*.fio; do
  name=$(basename "$p" .fio)
  /usr/bin/fio --parse-only "$p" >"/tmp/soak/parse-$name.log" 2>&1
  rc=$?; echo "IO-SOAK-PARSE $name $rc"; test "$rc" = 0 || fail parse
 done
run_job() {
  name=$1; mark "$name-begin"
  /bin/busybox setsid /usr/bin/fio "/fio/profiles/$name.fio" --output-format=json --output="/tmp/soak/$name.json" >"/tmp/soak/$name.log" 2>&1 &
  fio_pid=$!; pids="$pids $fio_pid"
  wait "$fio_pid"; rc=$?
  echo "IO-SOAK-EXIT $name $rc"; mark "$name-end"; test "$rc" = 0 || fail "$name"
  remove_pid "$fio_pid"
}
monitor() {
  sleep_pid=
  trap 'if test -n "$sleep_pid"; then kill "$sleep_pid" 2>/dev/null; wait "$sleep_pid" 2>/dev/null; fi; exit 0' TERM INT HUP
  while :; do
    read up rest < /proc/uptime
    for entry in cpu0:$cpu0 cpu1:$cpu1 vm0:$vm0; do
      name=${entry%%:*}; group=${entry#*:}
      /bin/busybox awk -v g="$group" -v n="$name" -v t="$up" '$5==g {count++;ticks+=$14+$15;if($24>rss)rss=$24;comm=substr($2,2,length($2)-2); printf "IO-SOAK-PROC name=%s uptime=%s group=%s pid=%s start=%s comm=%s ticks=%.0f rss_pages=%s\n",n,t,g,$1,$22,comm,$14+$15,$24; if ((n ~ /^cpu/ && comm=="stress-ng-cpu" && $14+$15>0) || (n=="vm0" && comm=="stress-ng-vm" && $14+$15>0 && $24>=65536)) print "ready" > ("/tmp/soak/ready-" n)} END {printf "IO-SOAK-LIVE name=%s uptime=%s group=%s count=%d ticks=%.0f rss_pages=%.0f\n",n,t,g,count,ticks,rss}' /proc/[0-9]*/stat 2>/dev/null
    done
    sleep 5 & sleep_pid=$!; wait "$sleep_pid"; sleep_pid=
  done
}
if test "$phase" = write; then
  test ! -e /bench/data-0.bin || fail reused-image
  run_job precondition0
  run_job unloaded
  run_job verify-before
  run_job precondition1
  @START_WORKERS@
  monitor >/tmp/soak/liveness.log & monitor_pid=$!
  readiness=0
  while test ! -f /tmp/soak/ready-cpu0 || test ! -f /tmp/soak/ready-cpu1 || test ! -f /tmp/soak/ready-vm0; do
    readiness=$((readiness+1)); test "$readiness" -le 30 || fail stress-readiness; sleep 1
  done
  run_job loaded
  kill "$monitor_pid" 2>/dev/null; wait "$monitor_pid" 2>/dev/null; monitor_pid=
  for name in cpu0 cpu1 vm0; do
    eval p=\$$name
    wait "$p"; rc=$?
    echo "IO-SOAK-EXIT $name $rc"; mark "$name-end"; test "$rc" = 0 || fail "$name"
    remove_pid "$p"
  done
  test -z "$(echo "$pids" | tr -d ' ')" || fail live-owned-processes
fi
run_job verify-all
for i in 0 1; do
  f=/bench/data-$i.bin; n=$(wc -c < "$f"); test "$n" = 67108864 || fail length
  hash=$(sha256sum "$f"); hash=${hash%% *}
  echo "IO-SOAK-DATA file=$i bytes=$n sha256=$hash"
done
sync || fail sync
mount -o remount,ro / || fail remount-ro
rootinfo=$(awk '$2 == "/" {print $1, $3, $4}' /proc/mounts)
case "$rootinfo" in "/dev/vda ext4 ro,"*) ;; *) fail final-root;; esac
echo IO-SOAK-ROOT-READONLY
export_logs || fail export
cat /proc/interrupts
dmesg
echo "IO-SOAK-PASS phase=$phase"
drain_console || fail console-drain
poweroff -f
fail poweroff-returned
'''
START = []
for name, options in STRESS_ARGS.items():
    START.append(f'mark {name}-begin\n/bin/busybox setsid /usr/bin/stress-ng {options} --timeout 360s --verify --metrics-brief > /tmp/soak/{name}.log 2>&1 &\n{name}=$!; pids="$pids ${name}"')
INIT = INIT.replace('@START_WORKERS@', '\n'.join(START))
# BusyBox applets are explicit where the original fixture has no PATH symlink.
INIT = INIT.replace(' | sed ', ' | /bin/busybox sed ').replace(' | tr ', ' | /bin/busybox tr ')


def prepare(args):
    pin = json.loads((args.matrix_inputs / 'inputs.json').read_text())
    for name, sha in pin['outputs'].items():
        if digest(args.matrix_inputs / name) != sha: raise ValueError('matrix input changed: ' + name)
    if admission.tree_manifest(args.matrix_inputs / 'rootfs') != pin['rootfs']: raise ValueError('matrix root tree changed')
    if digest(args.stress) != STRESS_SHA or digest(args.stress_source) != SOURCE_SHA: raise ValueError('stress binary/source pin')
    prior_manifest = args.stress.parent.parent / 'bin.sha256'
    if not any(line.startswith(STRESS_SHA + ' ') and line.endswith('/stress-ng') for line in prior_manifest.read_text().splitlines()): raise ValueError('retained stress build provenance')
    build_log = args.stress.parent.parent.parent / 'build.log'
    b = args.stress.read_bytes()
    if b[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', b, 18)[0] != 62: raise ValueError('not x86_64 ELF')
    ph, size, count = struct.unpack_from('<Q', b, 32)[0], *struct.unpack_from('<HH', b, 54)
    if any(struct.unpack_from('<I', b, ph + i * size)[0] in (2, 3) for i in range(count)): raise ValueError('not fully static stress binary')
    args.out.mkdir(parents=True, exist_ok=False); root = args.out / 'rootfs'
    shutil.copytree(args.matrix_inputs / 'rootfs', root, symlinks=True)
    shutil.rmtree(root / 'fio/profiles'); (root / 'fio/profiles').mkdir()
    shutil.copyfile(args.stress, root / 'usr/bin/stress-ng'); (root / 'usr/bin/stress-ng').chmod(0o755)
    (root / 'sbin/init').write_text(INIT)
    for name in PROFILES:
        (root / 'fio/profiles' / (name + '.fio')).write_text('\n'.join('[' + n + ']\n' + ''.join(f'{k}={v}\n' for k, v in o.items()) for n, o in jobs(name)))
    old_hashes = (root / 'fio/inputs.sha256').read_text().splitlines()
    hashes = [line for line in old_hashes if '/fio/profiles/' not in line and '/sbin/init' not in line]
    hashes += [digest(p) + '  /' + str(p.relative_to(root)) for p in [root / 'sbin/init', root / 'usr/bin/stress-ng', *sorted((root / 'fio/profiles').glob('*'))]]
    (root / 'fio/inputs.sha256').write_text('\n'.join(hashes) + '\n')
    for name in ('linux', 'bootstrap.cpio', 'mke2fs.conf'): shutil.copyfile(args.matrix_inputs / name, args.out / name)
    tools = pin['tools']
    for info in tools.values():
        if digest(info['path']) != info['sha256']: raise ValueError('native tool changed')
    image = args.out / 'root.raw'
    with image.open('xb') as f: f.truncate(DISK_BYTES)
    checked([tools['mke2fs']['path'], '-F', '-t', 'ext4', '-b', '4096', '-L', 'IOSOAK', '-E', 'root_owner=0:0', '-d', root, image], args.out / 'mke2fs.log', dict(os.environ, MKE2FS_CONFIG=str(args.out / 'mke2fs.conf')))
    edits = ''.join(f'set_inode_field /{p.relative_to(root)} {field} 0\n' for p in sorted(root.rglob('*')) for field in ('uid', 'gid'))
    (args.out / 'ownership.debugfs').write_text(edits)
    checked([tools['debugfs']['path'], '-w', '-f', args.out / 'ownership.debugfs', image], args.out / 'ownership.log')
    checked([tools['e2fsck']['path'], '-fn', image], args.out / 'initial-fsck.log')
    checked([tools['debugfs']['path'], '-R', 'stats', image], args.out / 'filesystem-stats.log')
    free = int(re.search(r'^Free blocks:\s+(\d+)', (args.out / 'filesystem-stats.log').read_text(), re.M)[1]) * 4096
    if free < 2 * FILE_BYTES + (32 << 20): raise ValueError('insufficient corpus reserve')
    oracle = args.out / 'oracle'; oracle.mkdir()
    for p in SOURCES: shutil.copyfile(p, oracle / p.name)
    write_json(args.out / 'inputs.json', {'purpose': 'five-minute verified CPU/memory and disk overlap; separate60s control',
        'matrix_inputs': {'path': str(args.matrix_inputs), 'sha256': digest(args.matrix_inputs / 'inputs.json')},
        'stress': {'binary': str(args.stress), 'sha256': STRESS_SHA, 'source': str(args.stress_source), 'source_sha256': SOURCE_SHA,
                   'prior_binary_manifest': {'path': str(prior_manifest), 'sha256': digest(prior_manifest)},
                   'prior_build_log': {'path': str(build_log), 'sha256': digest(build_log)}},
        'disk_bytes': DISK_BYTES, 'file_bytes': FILE_BYTES, 'free_bytes': free, 'tools': tools,
        'producer': {p.name: digest(p) for p in SOURCES}, 'profiles': {n: dict(jobs(n)) for n in PROFILES},
        'rootfs': admission.tree_manifest(root), 'outputs': {n: digest(args.out / n) for n in ('linux', 'bootstrap.cpio', 'root.raw', 'mke2fs.conf', 'rootfs/sbin/init')}})


def fio_result(data, profile):
    errors = []
    def require(ok):
        if not ok: raise ValueError('fio invariant failed')
    try:
        require(data['fio version'] == 'fio-3.39')
        require(len(data['jobs']) == 2)
        if profile in ('loaded', 'unloaded'):
            overlap = min(j['job_start'] + j['job_runtime'] for j in data['jobs']) - max(j['job_start'] for j in data['jobs'])
            require(overlap >= (299000 if profile == 'loaded' else 59000))
        for j, (name, opts) in zip(data['jobs'], jobs(profile)):
            require(j['jobname'] == name and j['error'] == 0)
            require({k: str(v) for k, v in j['job options'].items()} == opts)
            r, w = j['read']['io_bytes'], j['write']['io_bytes']
            if profile.startswith('precondition'): require(r == w == FILE_BYTES)
            elif profile.startswith('verify'): require(r == FILE_BYTES and w == 0)
            else:
                require(j['job_runtime'] >= (299000 if profile == 'loaded' else 59000))
                require((r > 0 and w == 0) if opts['rw'] == 'randread' else (w > 0 and r == 0))
            require(bool(j['iodepth_level']))
            for direction in ('read', 'write'):
                d = j[direction]
                require(d['short_ios'] == d['drop_ios'] == 0)
                require(d['io_bytes'] == d['total_ios'] * 4096)
                if d['io_bytes']:
                    require(all(math.isfinite(d[k]) and d[k] > 0 for k in ('total_ios', 'bw_bytes', 'iops', 'runtime')))
                    c = d['clat_ns']; require(c['N'] > 0 and math.isfinite(c['mean']) and c['mean'] >= 0)
                    require(all(k in c['percentile'] and math.isfinite(c['percentile'][k]) and c['percentile'][k] >= 0
                        for k in ('50.000000', '95.000000', '99.000000')))
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as e: errors.append('invalid fio result/options/verification: ' + str(e))
    return errors


def stress_result(text, name):
    kind = 'vm' if name == 'vm0' else 'cpu'
    errors = []
    def need(ok, why):
        if not ok: errors.append(why)
    need(re.findall(r'passed: (\d+): (\w+) \((\d+)\)', text) == [('1', kind, '1')], 'one passed worker')
    for label in ('failed', 'skipped'):
        values = re.findall(r'\b' + label + r': (\d+)', text)
        need(values == ['0'], label)
    values = [v.strip() for v in re.findall(r'\bmetrics[ -]untrustworthy:[ \t]*([^\r\n]*)', text)]
    need(values == ['0'], 'exactly one zero untrustworthy-metrics summary')
    metrics = re.findall(r'stress-ng:\s+metrc:\s+\[\s*\d+\]\s+' + kind + r'\s+(\d+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)', text)
    need(len(metrics) == 1 and int(metrics[0][0]) > 0 and float(metrics[0][1]) >= 350 and float(metrics[0][2]) + float(metrics[0][3]) > 0, 'verified work and CPU time')
    need('successful run completed' in text and not re.search(r'stress-ng:\s+(?:fail|error):', text), 'stress verification failure')
    return errors


def interval_result(content, kind, direction, runtime, total_ios):
    rows = []
    for line in content.splitlines():
        if not line.strip(): continue
        fields = [int(x.strip()) for x in line.split(',')]
        # fio3.39 stat.h:29 groups ×64 bins; its HOWTO's1216-bin claim is stale.
        expected = 3 + 29 * 64 if kind == 'clat_hist' else 5
        if len(fields) != expected or any(x < 0 for x in fields): raise ValueError('interval schema/nonnegative integers')
        d, bs = (fields[1], fields[2]) if kind == 'clat_hist' else (fields[2], fields[3])
        if d != direction or bs not in (0, 4096): raise ValueError('interval direction/block size')
        if kind != 'clat_hist' and fields[4] != 0: raise ValueError('unexpected priority')
        rows.append(fields)
    if len(rows) < 2: raise ValueError('insufficient interval observations')
    times = [r[0] for r in rows]
    if any(b < a for a, b in zip(times, times[1:])) or times[-1] > runtime + 5000: raise ValueError('interval clock/order')
    count = sum(sum(r[3:]) for r in rows) if kind == 'clat_hist' else None
    if count is not None and not 0 < count <= total_ios: raise ValueError('histogram completion count')
    return {'rows': len(rows), 'first_ms': times[0], 'last_ms': times[-1], 'max_gap_ms': max(b-a for a,b in zip(times,times[1:])),
            'histogram_completions': count, 'job_completions': total_ios,
            'note': 'actual retained samples; no assumption that every configured1s interval exists'}


def decode_logs(text):
    logs, errors, total = {}, [], 0
    pattern = r'^IO-SOAK-LOG-BEGIN v2 ([\w.-]+) bytes=(\d+) sha256=([a-f0-9]{64})\n(.*?)^IO-SOAK-LOG-END ([\w.-]+)$'
    for name, size, sha, payload, end in re.findall(pattern, text, re.M | re.S):
        try:
            size = int(size); total += size
            if name != end or name in logs: raise ValueError('log framing')
            if size > MAX_LOG_BYTES or total > MAX_TOTAL_LOG_BYTES: raise ValueError('decoded log bound')
            if len(payload) > 2 * MAX_LOG_BYTES: raise ValueError('encoded log bound')
            # Reject injected console text, truncation, extra gzip streams and bombs.
            wire = base64.b64decode(payload.replace('\n', ''), validate=True)
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            data = decoder.decompress(wire, size + 1)
            if len(data) != size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise ValueError('gzip framing/length')
            if hashlib.sha256(data).hexdigest() != sha: raise ValueError('log SHA256')
            logs[name] = data.decode('utf-8')
        except (ValueError, zlib.error) as e:
            errors.append(name + ': ' + str(e))
    return logs, errors


def encoded_log(name, content):
    """Synthetic host equivalent of the guest's lossless export, for controls."""
    data = content.encode('utf-8')
    wire = base64.encodebytes(gzip.compress(data, compresslevel=1, mtime=0)).decode('ascii')
    return f'IO-SOAK-LOG-BEGIN v2 {name} bytes={len(data)} sha256={hashlib.sha256(data).hexdigest()}\n{wire}IO-SOAK-LOG-END {name}'


def judge(text, phase):
    errors = []
    def need(ok, why):
        if not ok: errors.append(why)
    need(re.findall(r'^IO-SOAK-BEGIN phase=(\S+) cpus=(\d+) kernel=(\S+)$', text, re.M) == [(phase, '4', block.KVER)], 'guest identity')
    mode = 'rw' if phase == 'write' else 'ro'
    need(len(re.findall(r'^IO-SOAK-ROOT /dev/vda ext4 ' + mode + r',.*$', text, re.M)) == 1, 'root mode')
    need(re.findall(r'^IO-SOAK-PARSE (\S+) (\d+)$', text, re.M) == [(n, '0') for n in sorted(PROFILES)], 'profile parsing')
    executed = PROFILES if phase == 'write' else ('verify-all',)
    order = list(PROFILES[:-1]) + list(WORKERS) + ['verify-all'] if phase == 'write' else ['verify-all']
    need(re.findall(r'^IO-SOAK-EXIT (\S+) (\d+)$', text, re.M) == [(n, '0') for n in order], 'child statuses/order')
    expected = {'fio-version.txt', 'stress-version.txt', 'stress-help.txt'} | {f'parse-{n}.log' for n in PROFILES}
    expected |= {f'{n}.{s}' for n in executed for s in ('json', 'log')}
    if phase == 'write':
        expected |= {'liveness.log', 'ready-cpu0', 'ready-cpu1', 'ready-vm0'} | {n + '.log' for n in WORKERS}
        expected |= {f'{n}-{i}_{kind}.{i+1}.log' for n in ('loaded', 'unloaded') for i in range(2) for kind in ('bw', 'iops', 'clat_hist')}
    logs, export_errors = decode_logs(text)
    errors += export_errors
    need(set(logs) == expected, 'exact retained log set')
    for side in ('BEGIN', 'END'): need(len(re.findall(r'^IO-SOAK-LOG-' + side + r'(?: |$)', text, re.M)) == len(expected), 'log frame count')
    need(logs.get('fio-version.txt', '').strip() == 'fio-3.39', 'fio version')
    need(re.findall(r'stress-ng, version (\S+)', logs.get('stress-version.txt', '')) == ['0.19.02'], 'stress version')
    need(all(x in logs.get('stress-help.txt', '') for x in ('cpu-method', 'vm-method', 'vm-bytes', 'vm-keep', 'verify', 'seed', 'metrics-brief')), 'stress options available')
    results = {}
    for n in executed:
        try:
            data = json.loads(logs[n + '.json']); results[n] = data
            errors += [n + ': ' + e for e in fio_result(data, n)]
        except (KeyError, ValueError, TypeError) as e: errors.append('fio JSON: ' + str(e))
    marks = re.findall(r'^IO-MATRIX-MARK name=(\S+) uptime=([0-9.]+)$', text, re.M)
    times = dict(marks); need(len(times) == len(marks), 'duplicate time markers')
    if phase == 'write':
        try:
            start, end = float(times['loaded-begin']), float(times['loaded-end'])
            need(end - start >= 299, 'five-minute loaded fio interval')
            for n in WORKERS:
                need(float(times[n + '-begin']) <= start < end <= float(times[n + '-end']), n + ' launch/wait envelope (wait-end is not actual exit)')
                need(end - float(times[n + '-begin']) < 350, n + ' loaded completion precedes minimum observed worker runtime')
                errors += [n + ': ' + e for e in stress_result(logs[n + '.log'], n)]
                rows = re.findall(r'^IO-SOAK-LIVE name=' + n + r' uptime=([0-9.]+) group=(\d+) count=(\d+) ticks=(\d+) rss_pages=(\d+)$', logs['liveness.log'], re.M)
                need(len(rows) >= 50 and len({r[1] for r in rows}) == 1 and all(int(r[2]) > 0 for r in rows), n + ' observed process group liveness')
                need(rows and float(rows[0][0]) <= start + 8 and float(rows[-1][0]) >= end - 8, n + ' sampled overlap boundaries')
                need(all(0 < float(b[0]) - float(a[0]) <= 10 and int(b[3]) > int(a[3]) for a, b in zip(rows, rows[1:])), n + ' continuing CPU progress')
                if n == 'vm0': need(rows and max(int(r[4]) for r in rows) >= 65536, 'resident256MiB memory observed')
                procs = re.findall(r'^IO-SOAK-PROC name=' + n + r' uptime=([0-9.]+) group=(\d+) pid=(\d+) start=(\d+) comm=(\S+) ticks=(\d+) rss_pages=(\d+)$', logs['liveness.log'], re.M)
                by_pid = {}
                for row in procs:
                    if row[4] != ('stress-ng-vm' if n == 'vm0' else 'stress-ng-cpu'): continue
                    by_pid.setdefault((row[1], row[2], row[3]), []).append(row)
                need(any(len(rs) >= 50 and float(rs[0][0]) <= start + 8 and float(rs[-1][0]) >= end - 8
                    and all(0 < float(b[0])-float(a[0]) <= 10 and int(b[5]) > int(a[5]) for a,b in zip(rs,rs[1:]))
                    and (n != 'vm0' or max(int(r[6]) for r in rs) >= 65536) for rs in by_pid.values()), n + ' stable actual worker PID/starttime progressing during sampled overlap')
        except (KeyError, ValueError) as e: errors.append('overlap evidence: ' + str(e))
        for profile in ('loaded', 'unloaded'):
            summaries = {}
            for i in range(2):
                for kind in ('bw', 'iops', 'clat_hist'):
                    name = f'{profile}-{i}_{kind}.{i+1}.log'
                    try:
                        j = results[profile]['jobs'][i]
                        summaries[name] = interval_result(logs[name], kind, i, j['job_runtime'], j['read' if i == 0 else 'write']['total_ios'])
                    except (ValueError, KeyError, TypeError, IndexError) as e: errors.append(name + ': ' + str(e))
            if profile in results: results[profile]['interval_observations'] = summaries
    hashes = re.findall(r'^IO-SOAK-DATA file=(\d+) bytes=(\d+) sha256=([a-f0-9]{64})$', text, re.M)
    need([(i, n) for i, n, h in hashes] == [('0', str(FILE_BYTES)), ('1', str(FILE_BYTES))], 'complete file hashes')
    need(text.count('\nIO-SOAK-ROOT-READONLY\n') == 1 and re.findall(r'^IO-SOAK-PASS phase=(\S+)$', text, re.M) == [phase] and 'Power down' in text, 'clean root/S5 completion')
    need(not re.search(r'IO-SOAK-FAIL|VIRTIO-BLOCK-FAIL|BUG:|Oops:|Kernel panic|Call Trace:|EXT4-fs error|Buffer I/O error|end_fsync failed|Out of memory:|oom-kill:|Killed process|soft lockup|hung task|rcu:.*(?:stall|starved)', text + '\n' + '\n'.join(logs.values())), 'guest/kernel/I/O/OOM error')
    return errors, logs, results, [h for _, _, h in hashes]


def run(args):
    pin = json.loads((args.inputs / 'inputs.json').read_text())
    for name, h in pin['outputs'].items():
        if digest(args.inputs / name) != h: raise ValueError('input changed: ' + name)
    if pin['profiles'] != {n: dict(jobs(n)) for n in PROFILES}: raise ValueError('profile manifest')
    artifacts = {str(args.executable): digest(args.executable)}
    build_binding = None
    if args.backend == 'natemu':
        if any(p is None for p in (args.runtime, args.fexc, args.build_manifest)):
            raise ValueError('natemu requires runtime, fexc and build-manifest')
        artifacts.update({str(p): digest(p) for p in (args.runtime, args.fexc)})
        build = json.loads(args.build_manifest.read_text())
        if any(build['artifacts'].get(p) != h for p, h in artifacts.items()): raise ValueError('build binding')
        build_binding = {'path': str(args.build_manifest), 'sha256': digest(args.build_manifest)}
    else:
        version = subprocess.check_output([str(args.executable), '--version'], text=True, timeout=10)
        if not version.startswith('QEMU emulator version 11.1.2'): raise ValueError('QEMU version differs')
    fsck = Path(pin['tools']['e2fsck']['path'])
    if digest(fsck) != pin['tools']['e2fsck']['sha256']: raise ValueError('fsck changed')
    args.out.mkdir(parents=True, exist_ok=False)
    write_json(args.out / 'manifest.json', {'purpose': 'bounded verified I/O plus CPU/memory soak, not baseline matrix',
        'backend': args.backend, 'cpus': 4, 'artifacts': artifacts, 'build_manifest': build_binding,
        'input_manifest': {'path': str(args.inputs / 'inputs.json'), 'sha256': digest(args.inputs / 'inputs.json')},
        'oracle_sources': {str(p.resolve()): digest(p) for p in SOURCES},
        'bounds': {'vm_launches': 2, 'main_seconds': 900, 'verify_seconds': 120, 'log_bytes_per_vm': 32 << 20},
        'scope': ['two disjoint64MiB files, concurrent verified reads and CRC-tagged writes; final full CRC sweeps',
                  '60s unloaded then300s loaded, precondition reset between; fixed order/context, not randomized causal benchmark',
                  'two independent verified int64 CPU workers and one256MiB one-zero memory worker; each finite360s',
                  'lossless gzip/base64 v2 evidence export, max608 payload characters then50ms pause, after workload; prior bulk-serial RCU stall remains unexplained',
                  'actual-worker PID/starttime CPU observations sample overlap; shell wait-end is not actual worker exit; last timed memory pass may be incomplete',
                  'cross-engine context: natemu MMIO/single native request/F_FULLFSYNC versus QEMU PCI/threads/fsync; CPU profiles and translation differ',
                  'equal stress duration is not equal completed stress work; retain each worker metric and do not subtract loaded ratios as emulator overhead']})
    results, persisted = [], None
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        image = args.out / 'root.raw'; checked(['cp', '-c', args.inputs / 'root.raw', image], args.out / 'clone.log')
        if digest(image) != pin['outputs']['root.raw']: raise ValueError('clone differs')
        for phase in ('write', 'verify'):
            case = args.out / phase; case.mkdir(); before = digest(image)
            cmdline = f'console=ttyS0 earlyprintk=serial mitigations=off nokaslr root=/dev/vda io_phase={phase} io_rng=0'
            if args.backend == 'natemu':
                command = [str(args.executable), '--runtime', str(args.runtime), '--fexc', str(args.fexc), '--kernel', str(args.inputs / 'linux'),
                    '--initrd', str(args.inputs / 'bootstrap.cpio'), '--disk', str(image), '--vcpus', '4', '--ram', '2G',
                    '--compile-cache', 'off', '--serial', str(case / 'serial.log'), '--cmdline', cmdline]
            else:
                # Same reference machine/device/cache contract as the existing ordinary-disk matrix.
                command = [str(args.executable), '-machine', 'pc-i440fx-11.1', '-accel', 'tcg,thread=multi', '-cpu', 'max', '-smp', '4',
                    '-m', '2048', '-nodefaults', '-display', 'none', '-monitor', 'none', '-no-reboot', '-serial', 'file:' + str(case / 'serial.log'),
                    '-kernel', str(args.inputs / 'linux'), '-initrd', str(args.inputs / 'bootstrap.cpio'), '-append', cmdline,
                    '-drive', f'file={image},format=raw,if=none,id=disk,cache=writeback,aio=threads', '-device', matrix.baselines.DEVICE]
            result = matrix.execute(command, case, 900 if phase == 'write' else 120)
            serial = case / 'serial.log'; text = serial.read_text(errors='replace').replace('\r', '') if serial.exists() else ''
            errors, logs, data, hashes = judge(text, phase)
            exported = case / 'guest'; exported.mkdir()
            for name, content in logs.items(): (exported / name).write_text(content)
            if result['returncode'] != 0 or result['timeout'] or result['log_limit']: errors.append('process failure/timeout/log limit')
            if set(result['host_time']) != {'real', 'user', 'sys'}: errors.append('missing host CPU/wall')
            try: checked([fsck, '-fn', image], case / 'fsck.log')
            except RuntimeError as e: errors.append('offline fsck: ' + str(e))
            after = digest(image)
            if phase == 'write':
                if before == after: errors.append('main image unchanged')
                persisted = hashes
            elif hashes != persisted or before != after: errors.append('persisted hash or read-only image changed')
            result['limitations'] = ['An unexplained guest RCU stall occurred during prior bulk-serial export; compressed paced export does not establish a UART/runtime fix.',
                'Whole-VM CPU/wall includes boot, correctness checks and evidence export; fio job metrics describe workload intervals separately.']
            result.update(phase=phase, disk_before=before, disk_after=after, file_sha256=hashes, errors=errors, passed=not errors,
                          fio={n: d.get('jobs') for n, d in data.items()}, interval_observations={n: d.get('interval_observations') for n,d in data.items() if 'interval_observations' in d})
            write_json(case / 'result.json', result); results.append(result); write_json(args.out / 'results.json', results)
            print(json.dumps({'phase': phase, 'passed': not errors, 'seconds': result['seconds'], 'errors': errors}), flush=True)
            if errors: return 1
    return 0


def sample_fio(profile):
    rows = []
    for name, opts in jobs(profile):
        read = FILE_BYTES if profile.startswith(('verify', 'precondition')) else 4096 if opts['rw'] == 'randread' else 0
        write = FILE_BYTES if profile.startswith('precondition') else 4096 if opts['rw'] == 'randwrite' else 0
        def metric(n): return {'io_bytes': n, 'total_ios': n // 4096, 'short_ios': 0, 'drop_ios': 0,
            'bw_bytes': n, 'iops': n // 4096, 'runtime': 300000,
            'clat_ns': {'N': n // 4096, 'mean': 100, 'percentile': {'50.000000': 100, '95.000000': 200, '99.000000': 300}}}
        rows.append({'jobname': name, 'error': 0, 'job options': opts, 'job_start': 100000, 'job_runtime': 300000 if profile == 'loaded' else 60000,
            'read': metric(read), 'write': metric(write), 'iodepth_level': {'16': 100}})
    return {'fio version': 'fio-3.39', 'jobs': rows}


def sample_stress(name):
    kind = 'vm' if name == 'vm0' else 'cpu'
    return f'''stress-ng: metrc: [123] {kind} 100 360.00 100.00 20.00
stress-ng: info: [123] passed: 1: {kind} (1)
stress-ng: info: [123] failed: 0
stress-ng: info: [123] skipped: 0
stress-ng: info: [123] metrics untrustworthy: 0
stress-ng: info: [123] successful run completed in360s
'''


def sample_serial(phase, replacements=()):
    executed = PROFILES if phase == 'write' else ('verify-all',)
    logs = {'fio-version.txt': 'fio-3.39', 'stress-version.txt': 'stress-ng, version 0.19.02',
            'stress-help.txt': 'cpu-method vm-method vm-bytes vm-keep verify seed metrics-brief'}
    logs.update({f'parse-{n}.log': '' for n in PROFILES})
    for n in executed: logs.update({n + '.json': json.dumps(sample_fio(n)), n + '.log': ''})
    mode = 'rw' if phase == 'write' else 'ro'
    lines = [f'IO-SOAK-BEGIN phase={phase} cpus=4 kernel={block.KVER}', f'IO-SOAK-ROOT /dev/vda ext4 {mode},relatime']
    lines += [f'IO-SOAK-PARSE {n} 0' for n in sorted(PROFILES)]
    order = list(PROFILES[:-1]) + list(WORKERS) + ['verify-all'] if phase == 'write' else ['verify-all']
    lines += [f'IO-SOAK-EXIT {n} 0' for n in order]
    if phase == 'write':
        logs.update({n + '.log': sample_stress(n) for n in WORKERS})
        logs.update({f'ready-{n}': 'ready\n' for n in WORKERS})
        logs['liveness.log'] = '\n'.join(f'IO-SOAK-LIVE name={n} uptime={i*5+100:.2f} group={10+j} count=2 ticks={i+1} rss_pages=66000' for i in range(61) for j, n in enumerate(WORKERS))
        logs['liveness.log'] += '\n' + '\n'.join(f'IO-SOAK-PROC name={n} uptime={i*5+100:.2f} group={10+j} pid={20+j} start=1 comm=stress-ng-{"vm" if n=="vm0" else "cpu"} ticks={i+1} rss_pages=66000' for i in range(61) for j,n in enumerate(WORKERS))
        for n in ('loaded', 'unloaded'):
            for i in range(2):
                for k in ('bw', 'iops', 'clat_hist'):
                    rows = [[t, i, 4096] + [int(t == 1000)] + [0] * (29 * 64 - 1) if k == 'clat_hist' else [t, 100, i, 0, 0] for t in (1000, 2000)]
                    logs[f'{n}-{i}_{k}.{i+1}.log'] = '\n'.join(', '.join(map(str,row)) for row in rows)
        lines += [f'IO-MATRIX-MARK name={n}-begin uptime=99.00' for n in WORKERS]
        lines += ['IO-MATRIX-MARK name=loaded-begin uptime=100.00', 'IO-MATRIX-MARK name=loaded-end uptime=400.00']
        lines += [f'IO-MATRIX-MARK name={n}-end uptime=459.00' for n in WORKERS]
    for n, content in sorted(logs.items()):
        for before, after in replacements: content = content.replace(before, after, 1)
        lines.append(encoded_log(n, content))
    lines += [f'IO-SOAK-DATA file={i} bytes={FILE_BYTES} sha256=' + str(i) * 64 for i in range(2)]
    lines += ['IO-SOAK-ROOT-READONLY', f'IO-SOAK-PASS phase={phase}', 'reboot: Power down']
    return '\n' + '\n'.join(lines) + '\n'


def self_check():
    subprocess.run(['/bin/sh', '-n'], input=INIT, text=True, check=True)
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        program = re.search(r"awk -v g=\"\$group\" -v n=\"\$name\" -v t=\"\$up\" '(.+?)' /proc", INIT)[1]
        program = program.replace('/tmp/soak/ready-', temp + '/ready-')
        fields = ['0'] * 24; fields[0] = '123'; fields[1] = '(stress-ng-cpu)'; fields[4] = '10'; fields[13] = '5'; fields[21] = '1'; fields[23] = '200'
        output = subprocess.check_output(['awk', '-v', 'g=10', '-v', 'n=cpu0', '-v', 't=100.00', program], input=' '.join(fields) + '\n', text=True)
        assert 'pid=123 start=1 comm=stress-ng-cpu ticks=5' in output
        assert (Path(temp) / 'ready-cpu0').read_text().strip() == 'ready'
    count = 0
    for name in WORKERS:
        for label in ('metrics untrustworthy', 'metrics-untrustworthy'):
            good = sample_stress(name).replace('metrics untrustworthy', label)
            assert not stress_result(good, name)
            line = f'stress-ng: info: [123] {label}: 0\n'
            for bad in (good.replace(line, ''), good + line, good.replace(line, line.replace(': 0', ': 1')),
                        good + 'stress-ng: info: [123] metrics untrustworthy: 0\n'):
                assert stress_result(bad, name) == ['exactly one zero untrustworthy-metrics summary']
                count += 1
    for profile in PROFILES:
        good = sample_fio(profile); assert not fio_result(good, profile)
        for key, value in [('error', 1), ('jobname', 'wrong'), ('iodepth_level', {})]:
            bad = copy.deepcopy(good); bad['jobs'][0][key] = value; assert fio_result(bad, profile); count += 1
        for key in jobs(profile)[0][1]:
            bad = copy.deepcopy(good); bad['jobs'][0]['job options'][key] = 'wrong'; assert fio_result(bad, profile); count += 1
        bad = copy.deepcopy(good); bad['jobs'][0]['read']['io_bytes'] += 1; assert fio_result(bad, profile); count += 1
        bad = copy.deepcopy(good); bad['jobs'][0]['read']['iops'] = float('nan'); assert fio_result(bad, profile); count += 1
    for phase in ('write', 'verify'):
        text = sample_serial(phase); assert not judge(text, phase)[0], judge(text, phase)[0]
        for a, b in [('cpus=4', 'cpus=2'), ('IO-SOAK-EXIT verify-all 0', 'IO-SOAK-EXIT verify-all 1'),
            ('bytes=67108864', 'bytes=1'), ('IO-SOAK-ROOT-READONLY', 'missing'), ('Power down', 'no shutdown'),
            ('LOG-END fio-version.txt', 'LOG-END wrong.txt')]:
            assert judge(text.replace(a, b, 1), phase)[0]; count += 1
        for suffix in ('IO-SOAK-LOG-BEGIN extra.log\n', 'Out of memory: killed\n'):
            assert judge(text + suffix, phase)[0]; count += 1
    for profile in ('loaded', 'unloaded'):
        bad = sample_fio(profile); bad['jobs'][1]['job_start'] += 2000
        assert fio_result(bad, profile); count += 1
    for content, kind in [('not,a,log', 'bw'), ('1000, 1, 1, 0, 0\n2000, 1, 1, 0, 0', 'iops'), ('1000, 0, 4096, 1\n2000, 0, 4096, 1', 'clat_hist')]:
        try: interval_result(content, kind, 0, 300000, 100)
        except ValueError: count += 1
        else: raise AssertionError('malformed interval accepted')
    text = sample_serial('write')
    assert judge(text.replace('loaded-end uptime=400.00', 'loaded-end uptime=200.00'), 'write')[0]; count += 1
    for a, b in [('passed: 1: cpu (1)', 'passed: 0'),
                 ('skipped: 0', 'skipped: 1'), ('cpu 100 360.00', 'cpu 0 360.00'), ('cpu 100 360.00', 'cpu 100 60.00'),
                 ('count=2', 'count=0'), ('ticks=2 ', 'ticks=1 ')]:
        assert judge(sample_serial('write', [(a, b)]), 'write')[0]; count += 1
    frame = encoded_log('test.log', 'a' * 1000)
    assert decode_logs(frame) == ({'test.log': 'a' * 1000}, [])
    header, body = frame.split('\n', 1)
    payload = body.split('IO-SOAK-LOG-END')[0]
    compressed = base64.b64decode(payload)
    def wire_frame(data): return header + '\n' + base64.encodebytes(data).decode() + 'IO-SOAK-LOG-END test.log'
    for bad in (frame.replace('bytes=1000', 'bytes=1'), frame.replace('bytes=1000', 'bytes=8388609'),
                frame.replace('sha256=', 'sha256=0'), header + '\n!\n' + body,
                header + '\n' + body[4:], frame.replace('END test.log', 'END other.log'), frame + '\n' + frame,
                wire_frame(compressed + compressed), wire_frame(compressed[:-1]),
                wire_frame(compressed[:-8] + bytes([compressed[-8] ^ 1]) + compressed[-7:]),
                wire_frame(gzip.compress(b'b' * 1000)), wire_frame(gzip.compress(b'a' * 1001))):
        assert decode_logs(bad)[1] or not decode_logs(bad)[0]; count += 1
    # Console stalls remain fatal even if all compressed log frames decode.
    assert judge(sample_serial('verify') + 'rcu: INFO: rcu_preempt detected stalls on CPUs/tasks:\n', 'verify')[0]; count += 1
    assert judge(sample_serial('verify', [('fio-3.39', 'fio-3.39\nCall Trace:')]), 'verify')[0]; count += 1
    print(f'{count} negative controls, two valid serial cases and shell syntax pass; no image/VM/stress execution')


def main():
    p = argparse.ArgumentParser(description=__doc__); sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('self-check')
    a = sub.add_parser('prepare')
    for n in ('matrix-inputs', 'stress', 'stress-source', 'out'): a.add_argument('--' + n, type=Path, required=True)
    a = sub.add_parser('run')
    a.add_argument('--backend', choices=('natemu', 'qemu'), default='natemu')
    for n in ('executable', 'inputs', 'out', 'lock'): a.add_argument('--' + n, type=Path, required=True)
    for n in ('runtime', 'fexc', 'build-manifest'): a.add_argument('--' + n, type=Path)
    args = p.parse_args()
    for k, v in vars(args).items():
        if isinstance(v, Path): setattr(args, k, v.resolve())
    if args.action == 'self-check': return self_check()
    if args.action == 'prepare': return prepare(args)
    return run(args)


if __name__ == '__main__': raise SystemExit(main())
