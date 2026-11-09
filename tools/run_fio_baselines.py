#!/usr/bin/env python3
"""Bounded exploratory fio baselines; no downloads/builds or automatic retries.

QEMU runs the original installed-root write/restart oracle unchanged. Native uses
only filename/engine substitutions, checking full verified bytes and persistence.
All backends serialize through the same lock. Different engines, transports and
flush semantics make these whole-system observations, not isolated SSD scores.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import run_fio_admission as admission

write_json, digest, checked = admission.write_json, admission.digest, admission.checked
BYTES = 64 << 20
DEVICE = ('virtio-blk-pci,drive=disk,disable-legacy=on,num-queues=1,queue-size=128,'
          'indirect_desc=off,event_idx=off,packed=off,queue_reset=off,discard=off,'
          'write-zeroes=off,write-cache=on,config-wce=off,logical_block_size=512,physical_block_size=512')


def execute(command, case, seconds):
    write_json(case / 'command.json', command)
    env = dict(os.environ); env.pop('NATEMU_PERF_LOG', None)
    start = time.monotonic(); timed_out = limited = False
    def log_bytes(): return sum(f.stat().st_size for pattern in ('*.log', '*.json') for f in case.glob(pattern))
    with (case / 'stdout.log').open('wb') as out, (case / 'stderr.log').open('wb') as err:
        p = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, start_new_session=True)
        try:
            while p.poll() is None:
                timed_out = time.monotonic() - start >= seconds
                limited = log_bytes() > 64 << 20
                if timed_out or limited: admission.block.stop_group(p); break
                try: p.wait(timeout=0.25)
                except subprocess.TimeoutExpired: pass
        except BaseException: admission.block.stop_group(p); raise
    return {'process_seconds': time.monotonic() - start, 'returncode': p.returncode,
            'timeout': timed_out, 'log_limit': limited or log_bytes() > 64 << 20}


def native_result(data, phase):
    errors = []
    def need(ok, why):
        if not ok: errors.append(why)
    try:
        need(data['fio version'] == 'fio-3.39', 'fio version')
        need(len(data['jobs']) == 1, 'job count')
        j = data['jobs'][0]; options = j['job options']
        need(j['jobname'] == ('verify-write' if phase == 'write' else 'verify-read'), 'job name')
        need(j['error'] == 0, 'fio error')
        for key, value in {'ioengine': 'posixaio', 'direct': '1', 'iodepth': '16', 'bs': '4k', 'size': '64m', 'verify': 'crc32c', 'verify_fatal': '1'}.items():
            need(str(options.get(key)) == value, key)
        if phase == 'write': need(str(options.get('end_fsync')) == '1' and str(options.get('do_verify')) == '1', 'write verification/final sync')
        else: need(str(options.get('allow_file_create')) == '0', 'read must not create')
        need(j['read']['io_bytes'] == BYTES, 'full verification read')
        need(j['write']['io_bytes'] == (BYTES if phase == 'write' else 0), 'write byte count')
        need(bool(j.get('iodepth_level')), 'achieved depth missing')
    except (KeyError, IndexError, TypeError, ValueError) as e: errors.append(f'invalid fio JSON: {e}')
    return errors


def run(args):
    inputs = json.loads((args.inputs / 'inputs.json').read_text())
    for name, sha in inputs['outputs'].items():
        if digest(args.inputs / name) != sha: raise ValueError(f'input changed: {name}')
    args.out.mkdir(parents=True, exist_ok=False)
    executable = args.executable.resolve(strict=True)
    binding = None
    if args.backend == 'native':
        if args.binary_manifest is None: raise ValueError('native build manifest required')
        build = json.loads(args.binary_manifest.read_text())
        if Path(build['path']).resolve() != executable or build['sha256'] != digest(executable):
            raise ValueError('native binary differs from build manifest')
        binding = {'path': str(args.binary_manifest.resolve()), 'sha256': digest(args.binary_manifest)}
    version = subprocess.check_output([str(executable), '--version'], text=True, timeout=10)
    if args.backend == 'native' and version.strip() != 'fio-3.39': raise ValueError('native fio version')
    if args.backend == 'qemu' and not version.startswith('QEMU emulator version 11.1.2'): raise ValueError('QEMU version')
    write_json(args.out / 'manifest.json', {
        'purpose': 'exploratory same-host baseline, no pure emulator/SSD performance claim',
        'backend': args.backend, 'binary': {'path': str(executable), 'sha256': digest(executable), 'version': version},
        'native_build_manifest': binding,
        'input_manifest': {'path': str(args.inputs / 'inputs.json'), 'sha256': digest(args.inputs / 'inputs.json')},
        'oracle_sources': {str(p.resolve()): digest(p) for p in [Path(__file__), Path(admission.__file__), Path(admission.block.__file__), admission.ROOT / 'tools/run_virtio_rng.py']},
        'limits': {'processes': 2, 'seconds_per_process': 240 if args.backend == 'qemu' else 120},
        'differences': ['native APFS/arm64/posixaio/F_NOCACHE versus guest ext4/x86/io_uring/O_DIRECT',
                        'QEMU PCI device/profile and backend concurrency differ from natemu MMIO/one active request',
                        'native aio_fsync and QEMU fsync differ from natemu F_FULLFSYNC',
                        'no host cache purge; finite64MiB CRC workload, not sustained-storage benchmark']})
    for name, command in [('host', ['sw_vers']), ('machine', ['sysctl', 'hw.model', 'hw.ncpu', 'hw.memsize']),
                          ('thermal', ['pmset', '-g', 'therm']), ('filesystem', ['df', '-h', str(args.out)]),
                          ('binary', ['file', str(executable)])]:
        p = subprocess.run(command, capture_output=True, text=True, timeout=10)
        (args.out / (name + '.txt')).write_text(p.stdout + p.stderr)
    if args.backend == 'qemu':
        fsck = Path(inputs['tools']['e2fsck']['path'])
        if digest(fsck) != inputs['tools']['e2fsck']['sha256']: raise ValueError('fsck changed')
    results = []; persisted = None
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.backend == 'qemu':
            image = args.out / 'root.raw'
            checked(['cp', '-c', args.inputs / 'root.raw', image], args.out / 'clone.log')
            if digest(image) != inputs['outputs']['root.raw']: raise ValueError('clone differs')
        else:
            image = args.out / 'verify.bin'
            if image.exists(): raise ValueError('reused native input')
        for phase in ('write', 'verify'):
            case = args.out / phase; case.mkdir()
            before = digest(image) if image.exists() else None
            if args.backend == 'qemu':
                command = [str(executable), '-machine', 'pc-i440fx-11.1', '-accel', 'tcg,thread=multi', '-cpu', 'max',
                           '-smp', '2', '-m', '2048', '-nodefaults', '-display', 'none', '-monitor', 'none', '-no-reboot',
                           '-serial', 'file:' + str(case / 'serial.log'), '-kernel', str(args.inputs / 'linux'),
                           '-initrd', str(args.inputs / 'bootstrap.cpio'), '-append',
                           f'console=ttyS0 earlyprintk=serial mitigations=off nokaslr root=/dev/vda io_phase={phase} io_rng=0',
                           '-drive', f'file={image},format=raw,if=none,id=disk,cache=writeback,aio=threads', '-device', DEVICE]
                result = execute(command, case, 240)
                serial = case / 'serial.log'
                text = serial.read_text(errors='replace').replace('\r', '') if serial.exists() else ''
                errors, logs, data, content_hash = admission.judge(text, phase)
                exported = case / 'fio'; exported.mkdir()
                for name, content in logs.items(): (exported / name).write_text(content)
                if 'Power down' not in text: errors.append('guest poweroff missing')
                try: checked([fsck, '-fn', image], case / 'fsck.log')
                except RuntimeError as e: errors.append(str(e))
            else:
                name = 'verify-write.fio' if phase == 'write' else 'verify-read.fio'
                if digest(args.profiles / name) != inputs['rootfs']['fio/profiles/' + name]['sha256']:
                    raise ValueError('native profile differs from verified guest profile')
                original = (args.profiles / name).read_text()
                job = original.replace('filename=/bench/verify.bin', f'filename={image}').replace('ioengine=io_uring', 'ioengine=posixaio')
                assert job != original and 'ioengine=posixaio' in job
                (case / 'original.fio').write_text(original); (case / 'job.fio').write_text(job)
                command = [str(executable), str(case / 'job.fio'), '--output-format=json', '--output=' + str(case / 'fio.json')]
                result = execute(command, case, 120); content_hash = digest(image) if image.exists() else None
                try:
                    data = json.loads((case / 'fio.json').read_text()); errors = native_result(data, phase)
                except (OSError, ValueError) as e: data = None; errors = [str(e)]
                if not image.exists() or image.stat().st_size != BYTES: errors.append('file size')
            after = digest(image) if image.exists() else None
            if result['returncode'] or result['timeout'] or result['log_limit']: errors.append('process failure/timeout/log limit')
            if phase == 'write':
                persisted = content_hash
                if before == after: errors.append('write did not change data')
            elif content_hash != persisted or after != before: errors.append('read-only verification changed bytes/hash')
            if data is not None:
                try:
                    j = data['jobs'][0]
                    result['fio'] = {k:j[k] for k in ['job_runtime', 'elapsed', 'read', 'write', 'iodepth_level', 'usr_cpu', 'sys_cpu']}
                except (KeyError, IndexError, TypeError) as e: errors.append(f'missing/malformed fio metrics: {e}')
            result.update(phase=phase, before=before, after=after, file_sha256=content_hash, errors=errors, passed=not errors)
            write_json(case / 'result.json', result); results.append(result); write_json(args.out / 'results.json', results)
            print(json.dumps({k:result[k] for k in ['phase','process_seconds','passed','errors']}), flush=True)
            if errors: return 1
    return 0


def self_check():
    import copy
    count = 0
    for phase in ['write', 'verify']:
        options = dict(ioengine='posixaio', direct='1', iodepth='16', bs='4k', size='64m', verify='crc32c',
                       verify_fatal='1', do_verify='1', end_fsync='1', allow_file_create='0')
        data = {'fio version': 'fio-3.39', 'jobs': [{'jobname': 'verify-write' if phase == 'write' else 'verify-read',
                'error': 0, 'job options': options, 'read': {'io_bytes': BYTES},
                'write': {'io_bytes': BYTES if phase == 'write' else 0}, 'iodepth_level': {'16': 100}}]}
        assert not native_result(data, phase)
        for key, value in [('ioengine', 'sync'), ('direct', '0'), ('iodepth', '1'), ('verify', 'none'),
                           ('end_fsync' if phase == 'write' else 'allow_file_create', '0' if phase == 'write' else '1')]:
            bad = copy.deepcopy(data); bad['jobs'][0]['job options'][key] = value
            assert native_result(bad, phase); count += 1
        for key, value in [('error', 5), ('read', {'io_bytes': 4096}), ('write', {'io_bytes': BYTES - 4096}), ('iodepth_level', {})]:
            bad = copy.deepcopy(data); bad['jobs'][0][key] = value
            assert native_result(bad, phase); count += 1
    print(f'native oracle: {count} negative controls pass')


def main():
    if sys.argv[1:] == ['self-check']: self_check(); return 0
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backend', choices=['qemu', 'native'], required=True)
    for name in ['executable', 'inputs', 'out']: p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--profiles', type=Path, default=Path('/Users/liaozping/orca/natemu-raw/io-benchmark-prep-20261108-01/profiles'))
    p.add_argument('--binary-manifest', type=Path, help='required native build binary.json binding')
    p.add_argument('--lock', type=Path, default=Path.home()/'orca/natemu-raw/.vm.lock')
    args = p.parse_args()
    for name in ['inputs','out','profiles','lock']: setattr(args,name,getattr(args,name).resolve())
    return run(args)

if __name__ == '__main__': raise SystemExit(main())
