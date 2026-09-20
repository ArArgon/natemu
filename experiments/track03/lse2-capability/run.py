#!/usr/bin/env python3
"""Retained API-read fixture; --execute requires its own reviewed budget."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import sys
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
INPUTS = ('probe.c', 'run.py', 'README.md', 'entitlements.plist')
OPS = ('config_create', 'config_mmfr2', 'vm_create', 'vcpu_create',
       'vcpu_mmfr2', 'vcpu_destroy', 'vm_destroy', 'config_release')

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def classify(rows):
    if tuple(row.get('op') for row in rows) != OPS:
        raise ValueError('missing, duplicate or reordered operation')
    if rows[0].get('present') is not True or rows[-1].get('completed') is not True:
        raise ValueError('configuration lifetime incomplete')
    for row in rows[1:-1]:
        if type(row.get('rc')) is not int or not 0 <= row['rc'] <= 0xffffffff:
            raise ValueError('invalid API result')
        value = row.get('value')
        if not isinstance(value, str) or len(value) != 18 or not value.startswith('0x'):
            raise ValueError('invalid full register representation')
        int(value, 16)
    for i in (2, 3, 5, 6):
        if rows[i]['rc'] != 0:
            raise ValueError('creation or destruction failure')
    if rows[1]['rc'] != 0 or rows[4]['rc'] != 0:
        return dict(status='inconclusive_api_read_error')
    values = [int(rows[i]['value'], 16) for i in (1, 4)]
    fields = [(v >> 32) & 15 for v in values]
    if values[0] != values[1]:
        return dict(status='inconclusive_config_vcpu_mismatch', at=fields)
    return dict(status={0: 'lse2_not_enumerated', 1: 'lse2_enumerated_requires_findings_review'}.get(
        fields[0], 'inconclusive_unrecognized_at'), at=fields)

def self_check():
    rows = [dict(op=op, rc=0, value='0x0000000100000000') for op in OPS]
    rows[0] = dict(op=OPS[0], present=True)
    rows[-1] = dict(op=OPS[-1], completed=True)
    assert classify(rows)['status'] == 'lse2_enumerated_requires_findings_review'
    for val, expected in ((0, 'lse2_not_enumerated'), (2, 'inconclusive_unrecognized_at')):
        altered = [dict(row) for row in rows]
        for i in (1, 4): altered[i]['value'] = f'0x{val << 32:016x}'
        assert classify(altered)['status'] == expected
    altered = [dict(row) for row in rows]; altered[4]['rc'] = 1
    assert classify(altered)['status'] == 'inconclusive_api_read_error'
    altered = [dict(row) for row in rows]; altered[4]['value'] = '0x0000000100000001'
    assert classify(altered)['status'] == 'inconclusive_config_vcpu_mismatch'
    for altered in (rows[:-1], rows + [rows[0]], list(reversed(rows))):
        try: classify(altered)
        except ValueError: pass
        else: raise AssertionError('invalid sequence admitted')
    print('classifier synthetic checks passed; no Hypervisor calls')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name')
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if args.self_check:
        self_check(); return
    if not args.name: parser.error('--name is required')
    helper = REPO / 'experiments/track01/source/run.py'
    retention = load(helper, 'initial_retention')
    run = retention.allocate(os.environ.get('NATEMU_RAW_ROOT'), REPO, args.name)
    source = run / 'source'; source.mkdir()
    for name in INPUTS: shutil.copyfile(HERE / name, source / name)
    shutil.copyfile(helper, source / 'retention.py')
    record = dict(schema='natemu.track03.lse2-api-read.v1', status='preparing', commands=[],
                  execute_requested=args.execute, started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  inherited_cpu_limit=list(resource.getrlimit(resource.RLIMIT_CPU)), cpu_setter='omitted_for_probe',
                  budget=dict(workers=1, vms=1, vcpus=1, guest_bytes=0, vcpu_runs=0,
                              worker_seconds=20, execution_seconds=30, build_jobs=1),
                  inputs={n: hashlib.sha256((source / n).read_bytes()).hexdigest()
                          for n in INPUTS + ('retention.py',)})
    try:
        for name in INPUTS:
            if (HERE / name).read_bytes() != (source / name).read_bytes():
                raise RuntimeError('snapshot changed: ' + name)
        if helper.read_bytes() != (source / 'retention.py').read_bytes():
            raise RuntimeError('retention snapshot changed')
        retention = load(source / 'retention.py', 'retained_retention')
        for cmd in (['sw_vers'], ['uname', '-a'], ['sysctl', 'hw.model'],
                    ['xcrun', 'clang', '--version'], ['xcrun', '--show-sdk-version']):
            retention.command(run, record, cmd)
        sdk = retention.command(run, record, ['xcrun', '--show-sdk-path']).strip()
        retention.command(run, record, [sys.executable, '-B', source / 'run.py', '--self-check'])
        retention.command(run, record, ['xcrun', 'clang', '-arch', 'arm64', '-isysroot', sdk,
            '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', source / 'probe.c',
            '-framework', 'Hypervisor', '-o', run / 'probe'], timeout=60)
        record['status'] = 'built_not_signed_or_executed'
        if args.execute:
            if resource.getrlimit(resource.RLIMIT_CPU) != (resource.RLIM_INFINITY, resource.RLIM_INFINITY):
                raise RuntimeError('inherited VM CPU limit must be unlimited')
            start = time.monotonic()
            for argv in (['codesign', '--force', '--sign', '-', '--entitlements', source / 'entitlements.plist', run / 'probe'],
                         ['codesign', '--verify', '--verbose=2', run / 'probe']):
                remaining = 30 - (time.monotonic() - start)
                if remaining <= 0: raise TimeoutError('execution budget')
                retention.command(run, record, argv, timeout=min(20, remaining))
            remaining = 30 - (time.monotonic() - start)
            if remaining <= 0: raise TimeoutError('execution budget')
            raw = retention.command(run, record, [run / 'probe'], timeout=min(20, remaining), cpu_limit=False)
            record['assessment'] = classify([json.loads(line) for line in raw.splitlines()])
            record['status'] = 'collected_requires_findings_review'
    except BaseException as exc:
        record['status'] = 'failed'; record['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        record['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        retention.save(run, record); retention.checksums(run); print(run)

if __name__ == '__main__': main()
