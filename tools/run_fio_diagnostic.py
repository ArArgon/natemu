#!/usr/bin/env python3
"""One observed fio attempt, not admission or performance evidence.

prepare reuses the admission image builder with a diagnostic init. run is one N2
VM, <=180s total and <=120s after fio-start, <=64MiB logs. No builds/retries.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import time

import run_fio_admission as admission
from run_fio_admission import digest, write_json, checked

PROFILE_SHA = '8b8a23dcdb01ff6a1c1188a09616729bc952167977fac195b1e3f1b19b4909f1'
INIT = admission.INIT[:admission.INIT.index('sha256sum -c /fio/inputs.sha256')] + r'''
phase_mark() { echo "FIO-DIAG-PHASE $1 uptime=$(cat /proc/uptime)"; }
phase_mark version-begin
/usr/bin/fio --version || fail version
phase_mark version-end
printf '%s\n' '383b58e9f8fc2a0bfc9270fae73ac9491eefd0b375da9b79c57a7abeeff566ef  /usr/bin/fio' '8b8a23dcdb01ff6a1c1188a09616729bc952167977fac195b1e3f1b19b4909f1  /fio/profiles/verify-write.fio' | sha256sum -c - || fail diagnostic-input-hash
: >/tmp/fio/status.jsons
monitor() {
  seq=0
  while kill -0 "$fio_pid" 2>/dev/null; do
    echo "FIO-DIAG-HEARTBEAT-BEGIN $seq uptime=$(cat /proc/uptime)"
    echo "BLOCK $(cat /sys/block/vda/stat)"
    for d in /proc/[0-9]*; do
      test "$(cat "$d/comm" 2>/dev/null)" = fio || continue
      echo "PROC $d"
      cat "$d/stat" "$d/io" 2>/dev/null
      for t in "$d"/task/*; do echo "TASK $t"; cat "$t/stat" 2>/dev/null; done
    done
    echo "FIO-DIAG-HEARTBEAT-END $seq"
    cp /tmp/fio/status.jsons /tmp/fio/snapshot
    hash=$(sha256sum /tmp/fio/snapshot); hash=${hash%% *}
    echo "FIO-DIAG-SNAPSHOT-BEGIN $seq $hash"
    /bin/busybox base64 /tmp/fio/snapshot
    echo "FIO-DIAG-SNAPSHOT-END $seq"
    seq=$((seq + 1)); sleep 5
  done
}
test ! -e /bench/verify.bin || fail reused-image
phase_mark fio-start
/usr/bin/fio /fio/profiles/verify-write.fio --output-format=json --output=/tmp/fio/status.jsons --status-interval=5 --eta=always --eta-interval=5 --eta-newline=5 &
fio_pid=$!
monitor &
monitor_pid=$!
wait "$fio_pid"; rc=$?
kill "$monitor_pid" 2>/dev/null
wait "$monitor_pid" 2>/dev/null
phase_mark fio-end
echo "FIO-DIAG-EXIT $rc"
echo FIO-DIAG-FINAL-JSON-BEGIN
cat /tmp/fio/status.jsons
echo FIO-DIAG-FINAL-JSON-END
test "$rc" = 0 || fail fio
phase_mark sync-begin
sync || fail sync
phase_mark sync-end
mount -o remount,ro / || fail remount-ro
phase_mark root-readonly
sha256sum /bench/verify.bin
phase_mark poweroff
poweroff -f
fail poweroff-returned
'''


def prepare(args):
    profile = args.fio_prep / 'profiles/verify-write.fio'
    if digest(profile) != PROFILE_SHA: raise ValueError('original job changed')
    admission.INIT = INIT
    admission.prepare(args)
    p = args.out / 'inputs.json'; data = json.loads(p.read_text())
    data['purpose'] = 'diagnostic-only progress observation; not original admission'
    data['producer'][str(Path(__file__).resolve())] = digest(__file__)
    data['profile_sha256'] = PROFILE_SHA
    write_json(p, data)


def descendants(parent, fexc):
    lines = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,command='], text=True, timeout=5).splitlines()
    records = {}
    for line in lines:
        fields = line.strip().split(None, 2)
        if len(fields) == 3: records[int(fields[0])] = (int(fields[1]), fields[2])
    children = {parent}
    for _ in range(8):
        found = {pid for pid, (ppid, _) in records.items() if ppid in children}
        if found <= children: break
        children |= found
    return [parent] + sorted(pid for pid in children - {parent}
        if records[pid][1].startswith(str(fexc.resolve()) + ' --serve'))[:2]


def snapshots(text):
    import base64
    import hashlib
    rows = []
    for seq, sha, payload, end in re.findall(r'^FIO-DIAG-SNAPSHOT-BEGIN (\d+) ([a-f0-9]{64})\n(.*?)^FIO-DIAG-SNAPSHOT-END (\d+)$', text, re.M | re.S):
        row = {'sequence': int(seq), 'valid': False, 'objects': []}
        try:
            if seq != end: raise ValueError('frame sequence')
            raw = base64.b64decode(''.join(payload.split()), validate=True)
            if hashlib.sha256(raw).hexdigest() != sha: raise ValueError('snapshot hash')
            rest = raw.decode(); decoder = json.JSONDecoder()
            while rest.strip():
                rest = rest.lstrip()
                try: obj, n = decoder.raw_decode(rest)
                except ValueError: break  # Retained partial trailing status is not a complete observation.
                row['objects'].append(obj); rest = rest[n:]
            row.update(valid=True, trailing_bytes=len(rest.encode()))
        except (ValueError, UnicodeError) as error: row['error'] = str(error)
        rows.append(row)
    return rows


def run(args):
    inputs = json.loads((args.inputs / 'inputs.json').read_text())
    if inputs.get('profile_sha256') != PROFILE_SHA: raise ValueError('not diagnostic input')
    for name, sha in inputs['outputs'].items():
        if digest(args.inputs / name) != sha: raise ValueError(f'input changed: {name}')
    previous = json.loads(args.build_manifest.read_text())
    artifacts = {str(p.resolve()): digest(p) for p in (args.natemu, args.runtime, args.fexc)}
    if any(previous['artifacts'].get(p) != sha for p, sha in artifacts.items()): raise ValueError('build artifact changed')
    args.out.mkdir(parents=True, exist_ok=False)
    write_json(args.out / 'manifest.json', {'purpose': 'diagnostic, not admission/performance', 'inputs': inputs,
        'artifacts': artifacts, 'build_manifest': str(args.build_manifest.resolve()), 'build_manifest_sha256': digest(args.build_manifest),
        'oracle': {str(Path(p).resolve()): digest(p) for p in (__file__, admission.__file__, admission.block.__file__)},
        'max_seconds': 180, 'max_seconds_after_fio_start': args.fio_seconds,
        'host_perf': args.host_perf, 'max_log_bytes': 64 << 20})
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        image = args.out / 'root.raw'; checked(['cp', '-c', args.inputs / 'root.raw', image], args.out / 'clone.log')
        command = [str(args.natemu.resolve()), '--runtime', str(args.runtime.resolve()), '--fexc', str(args.fexc.resolve()),
            '--kernel', str((args.inputs / 'linux').resolve()), '--initrd', str((args.inputs / 'bootstrap.cpio').resolve()),
            '--disk', str(image.resolve()), '--vcpus', '2', '--ram', '2G', '--compile-cache', 'off',
            '--serial', str((args.out / 'serial.log').resolve()), '--cmdline',
            'console=ttyS0 earlyprintk=serial mitigations=off nokaslr root=/dev/vda io_phase=write io_rng=0']
        if args.host_perf:
            command += ['--perf-log', str((args.out / 'host-perf.log').resolve())]
        write_json(args.out / 'command.json', command)
        start = time.monotonic(); fio_start = None; phase_seen = set(); samples = []; sample_sets = 0; next_ps = 0; reason = None
        env = dict(os.environ); env.pop('NATEMU_PERF_LOG', None)
        with (args.out / 'stdout.log').open('wb') as stdout, (args.out / 'stderr.log').open('wb') as stderr, (args.out / 'observations.jsonl').open('w') as observations:
            def record(value):
                observations.write(json.dumps({'host_seconds': time.monotonic() - start, **value}) + '\n'); observations.flush()
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=env, start_new_session=True)
            try:
                while process.poll() is None:
                    now = time.monotonic(); serial = args.out / 'serial.log'
                    text = serial.read_text(errors='replace').replace('\r', '') if serial.exists() else ''
                    for match in re.finditer(r'^FIO-DIAG-PHASE (\S+) uptime=(.*)$', text, re.M):
                        if match.start() in phase_seen: continue
                        phase_seen.add(match.start()); record({'phase': match[1], 'guest_uptime': match[2], 'serial_character_offset': match.start()})
                        if match[1] == 'fio-start': fio_start = now
                    if now - start >= 180 or (fio_start is not None and now - fio_start >= args.fio_seconds): reason = 'observation deadline'; break
                    if sum(p.stat().st_size for p in args.out.iterdir() if p.is_file() and p.name != 'root.raw') > 60 << 20: reason = '60MiB live-log bound (4MiB final-summary reserve)'; break
                    if now >= next_ps:
                        pids = descendants(process.pid, args.fexc)
                        try: cpu = subprocess.check_output(['ps', '-p', ','.join(map(str, pids)), '-o', 'pid=,ppid=,time=,pcpu=,state=,comm='], text=True, timeout=5)
                        except subprocess.CalledProcessError as e: cpu = e.output
                        record({'pids': pids, 'ps': cpu}); next_ps = now + 5
                    if fio_start is not None and (sample_sets == 0 or (sample_sets == 1 and now - fio_start >= 30)):
                        for pid in descendants(process.pid, args.fexc):
                            dest = args.out / f'sample-{sample_sets}-{pid}.txt'
                            cmd = ['/usr/bin/sample', str(pid), '2', '10', '-mayDie', '-file', str(dest)]
                            log = dest.with_suffix('.log').open('wb')
                            child = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                            samples.append((child, log, time.monotonic(), cmd)); record({'sample_command': cmd})
                        sample_sets += 1
                    for child, log, began, cmd in samples:
                        if child.poll() is None and time.monotonic() - began > 15: admission.block.stop_group(child)
                    time.sleep(0.2)
            except Exception as error:
                reason = f'observer error: {type(error).__name__}: {error}'
                record({'observer_error': reason})
            finally:
                if process.poll() is None: admission.block.stop_group(process)
                for child, log, began, cmd in samples:
                    if child.poll() is None: admission.block.stop_group(child)
                    log.close(); record({'sample_command': cmd, 'returncode': child.returncode})
        text = (args.out / 'serial.log').read_text(errors='replace').replace('\r', '') if (args.out / 'serial.log').exists() else ''
        summaries = []
        for row in snapshots(text):
            objects = row.pop('objects')
            row['progress'] = [{'timestamp_ms': obj.get('timestamp_ms'), 'jobs': [
                {'jobname': j.get('jobname'), 'error': j.get('error'),
                 'read': {k: j.get('read', {}).get(k) for k in ('io_bytes', 'total_ios', 'runtime')},
                 'write': {k: j.get('write', {}).get(k) for k in ('io_bytes', 'total_ios', 'runtime')}}
                for j in obj.get('jobs', [])]} for obj in objects]
            summaries.append(row)
        write_json(args.out / 'snapshots.json', summaries)
        perf = {'enabled': args.host_perf, 'complete': False}
        if args.host_perf:
            try:
                rows = [json.loads(line[5:]) for line in (args.out / 'host-perf.log').read_text().splitlines() if line.startswith('vcpu ')]
                perf.update(records=rows, complete=len(rows) == 2 and {r['vcpu'] for r in rows} == {0, 1}
                    and all(r['log']['failed'] == r['log']['dropped'] == 0 for r in rows))
            except (OSError, ValueError, KeyError, TypeError) as error:
                perf['error'] = str(error)
        write_json(args.out / 'host-perf-summary.json', perf)
        log_bytes = sum(p.stat().st_size for p in args.out.iterdir() if p.is_file() and p.name != 'root.raw')
        result = {'purpose': 'diagnostic only', 'returncode': process.returncode, 'stop_reason': reason,
            'seconds': time.monotonic() - start, 'fio_started': fio_start is not None,
            'fio_exit': re.findall(r'^FIO-DIAG-EXIT (\d+)$', text, re.M), 'phase_markers': sorted(phase_seen),
            'disk_after': digest(image), 'admission_pass': False, 'host_perf_complete': perf['complete'],
            'log_bytes_before_result': log_bytes, 'logs_within_bound': log_bytes + 4096 <= 64 << 20}
        write_json(args.out / 'result.json', result); print(json.dumps(result), flush=True)
        return 0 if process.returncode == 0 and reason is None else 1


def self_check():
    import base64
    import hashlib
    subprocess.run(['sh', '-n'], input=INIT, text=True, check=True)
    assert '--parse-only' not in INIT and 'sha256sum -c /fio/inputs.sha256' not in INIT
    raw = b'{"jobs":[]}\n{"partial":'; sha = hashlib.sha256(raw).hexdigest()
    frame = f'FIO-DIAG-SNAPSHOT-BEGIN 0 {sha}\n{base64.b64encode(raw).decode()}\nFIO-DIAG-SNAPSHOT-END 0\n'
    assert snapshots(frame)[0]['objects'] == [{'jobs': []}] and snapshots(frame)[0]['trailing_bytes'] > 0
    assert not snapshots(frame.replace(sha, '0' * 64))[0]['valid']
    assert not snapshots(frame.replace('END 0', 'END 1'))[0]['valid']
    print('Diagnostic shell and snapshot negative controls pass; no image/VM/sample.')


def main():
    p = argparse.ArgumentParser(description=__doc__); sub = p.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    for name in ('fio-prep', 'block-inputs', 'out'): prep.add_argument('--' + name, type=Path, required=True)
    r = sub.add_parser('run')
    for name in ('natemu', 'runtime', 'fexc', 'inputs', 'out', 'lock', 'build-manifest'): r.add_argument('--' + name, type=Path, required=True)
    r.add_argument('--host-perf', action='store_true', help='existing host accounting; changes observation overhead, not performance evidence')
    r.add_argument('--fio-seconds', type=int, choices=(60, 120), default=120)
    sub.add_parser('self-check'); args = p.parse_args()
    return self_check() if args.command == 'self-check' else prepare(args) if args.command == 'prepare' else run(args)


if __name__ == '__main__': raise SystemExit(main())
