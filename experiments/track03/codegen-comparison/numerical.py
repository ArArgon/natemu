#!/usr/bin/env python3
"""Retain a bounded standalone native ARM64 numerical-kernel comparison."""
import argparse
import decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import statistics
import struct
import sys
import time
import uuid

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]

def load(path):
    spec = importlib.util.spec_from_file_location('retention', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def extended(signexp, significand):
    n = int(significand, 16)
    if not n: return decimal.Decimal(0)
    return decimal.Decimal((-1 if signexp & 32768 else 1)*n) * decimal.Decimal(2)**((signexp & 32767)-16383-63)

def reference_sin(x):
    # |x| <= 8: no argument-reduction constant or external numeric library.
    # Alternating Taylor eventually decreases; terminate far below ext80 ulps.
    term = total = x
    for k in range(1, 500):
        term *= -x*x / decimal.Decimal((2*k)*(2*k+1))
        total += term
        if abs(term) < decimal.Decimal('1e-105'): return total
    raise ValueError('reference did not converge')

def analyse(run):
    decimal.getcontext().prec = 120
    rows = [json.loads(x) for x in (run/'outputs/results.jsonl').read_text().splitlines()]
    accuracy = []
    outcomes = {}
    for r in rows:
        if r['kind'] != 'accuracy': continue
        x = extended(r['input80_signexp'], r['input80_signif'])
        target = decimal.Decimal(1) if r['operation']=='cancel' else reference_sin(x)
        b64 = decimal.Decimal.from_float(struct.unpack('>d', bytes.fromhex(r['binary64']))[0])
        b80 = extended(r['ext80_signexp'], r['ext80_signif'])
        accuracy.append(dict(operation=r['operation'], id=r['id'], exact_input=str(x), reference=str(target),
                             binary64_absolute_error=str(abs(b64-target)), ext80_absolute_error=str(abs(b80-target)),
                             binary64_value=str(b64), ext80_value=str(b80)))
        outcomes[(r['operation'],r['id'])] = (int(r['binary64'],16), int(r['ext80_signif'],16)^r['ext80_signexp'])
    if len(accuracy)!=26: raise ValueError('missing accuracy rows')
    timing = [r for r in rows if r['kind']=='timing']
    if len(timing)!=56: raise ValueError('missing timing rows')
    for r in timing:
        operation = 'cancel' if r['variant']<2 else 'sin'
        count = 12 if operation=='cancel' else 14
        values = [outcomes[(operation,i)][r['variant']%2] for i in range(count)]
        expected = (sum(values)*(r['n']//count)+sum(values[:r['n']%count])) & ((1<<64)-1)
        if r['checksum'] != f'{expected:016x}' or r['ns']<=0: raise ValueError('timed outputs or clock invalid')
    medians = []
    for length in range(2):
        for variant in range(4):
            values=[r['ns']/r['n'] for r in timing if r['length']==length and r['variant']==variant]
            medians.append(dict(length=length, variant=variant, median_ns=statistics.median(values),min_ns=min(values),max_ns=max(values)))
    (run/'analysis.json').write_text(json.dumps(dict(accuracy=accuracy,timing=medians,checksum_checks=len(timing),
      limitations=['standalone Linux ARM64 kernels, not captive runtime', 'binary64 cancellation is a native control, not generator output',
                   'finite sin path omits FEX profiling/state wrapper', 'no exception-flag or Intel hardware qualification',
                   'small paired batches, no independent timing replication']),indent=2)+'\n')

def main():
    p=argparse.ArgumentParser(); p.add_argument('--origin',type=Path,required=True); p.add_argument('--name',required=True); a=p.parse_args()
    retention=load(REPO/'experiments/track01/source/run.py')
    run=retention.allocate(os.environ.get('NATEMU_RAW_ROOT'),REPO,a.name)
    source=run/'source'; source.mkdir(); (run/'outputs').mkdir()
    record=dict(schema='natemu.track03.numerical.v1',purpose='exploratory standalone kernels',commands=[],status='preparing',origin=str(a.origin),started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
    expected={line.split('  ',1)[1]:line.split('  ',1)[0] for line in (a.origin/'SHA256SUMS').read_text().splitlines()}
    def copy_origin(relative,destination):
        src=a.origin/relative
        if hashlib.sha256(src.read_bytes()).hexdigest()!=expected[relative]: raise ValueError('sealed input mismatch '+relative)
        target=source/destination; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(src,target)
    origin=json.loads((a.origin/'manifest.json').read_text()); record['image']=origin['image']; record['fex_pin']=origin['pin']
    for file in ('numerical.py','numerical.cpp'): shutil.copyfile(HERE/file,source/file)
    shutil.copyfile(REPO/'experiments/track01/source/run.py',source/'retention.py'); retention=load(source/'retention.py')
    for lib in ('SoftFloat-3e/libsoftfloat_3e.a','cephes/libcephes_128bit.a'):
        copy_origin('build/External/'+lib,Path(lib).name)
    for component in ('SoftFloat-3e','cephes'):
        prefix='upstream/External/'+component+'/include/'
        for path in sorted((a.origin/prefix).rglob('*')):
            if path.is_file(): copy_origin(str(path.relative_to(a.origin)), 'include/'+str(path.relative_to(a.origin/prefix)))
    for rel in ('source/dependencies.json','build/compile_commands.json','upstream/FEXCore/Source/Common/SoftFloat.h',
                'upstream/FEXCore/Source/Interface/Core/Interpreter/Fallbacks/F80Fallbacks.h',
                'upstream/External/cephes/src/128bit/sinll.c','upstream/External/SoftFloat-3e/src/extF80_add.c','upstream/External/SoftFloat-3e/src/extF80_sub.c'):
        copy_origin(rel,'evidence/'+rel)
    script='''#!/bin/sh
set -eu
cd /work
uname -a > outputs/platform.txt
cat /proc/cpuinfo >> outputs/platform.txt
clang++ --version > outputs/toolchain.txt
ldd --version > outputs/libc.txt
clang++ -std=c++20 -O3 -fno-fast-math -ffp-contract=off -fno-builtin-sin '-DFEXCORE_PRESERVE_ALL_ATTR=__attribute__((preserve_all))' -I source/include -I source/include/SoftFloat-3e source/numerical.cpp source/libcephes_128bit.a source/libsoftfloat_3e.a -lm -o outputs/numerical
ldd outputs/numerical > outputs/linked.txt
sha256sum /lib/aarch64-linux-gnu/libm.so.6 /lib/aarch64-linux-gnu/libc.so.6 > outputs/runtime-libraries.sha256
llvm-objdump -d outputs/numerical > outputs/numerical.disassembly.txt
timeout 90 outputs/numerical > outputs/results.jsonl
'''
    (source/'numerical.sh').write_text(script)
    name='natemu-numerical-'+uuid.uuid4().hex; created=False; deadline=time.monotonic()+180
    def command(argv,seconds=30):
        left=deadline-time.monotonic()
        if left<=0: raise TimeoutError('numerical campaign execution budget')
        return retention.command(run,record,argv,timeout=min(seconds,left),cpu_limit=False)
    try:
        retention.save(run,record); created=True
        command(['docker','create','--name',name,'--network=none','--cpus=2','--memory=2g','--memory-swap=2g','--pids-limit=128','--user=0','--tmpfs','/var/lib/postgresql/data','--mount',f'type=bind,src={run},dst=/work','--entrypoint=/bin/sh',record['image'],'/work/source/numerical.sh'])
        command(['docker','start','--attach',name],150)
        state=json.loads(command(['docker','inspect',name]))[0]['State']
        if state['Running'] or state['ExitCode'] or state['OOMKilled']: raise RuntimeError('container failed '+str(state))
        # Analysis uses the retained script as well as the retained kernels.
        load(source/'numerical.py').analyse(run)
        record['status']='completed_exploratory'
    except BaseException as error:
        record['status']='failed'; record['error']=repr(error); raise
    finally:
        if created:
            try: retention.command(run,record,['docker','rm','--force','--volumes',name],timeout=30,cpu_limit=False)
            except Exception as error: record['cleanup_error']=repr(error); record['status']='failed_cleanup'
        record['finished_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()); retention.save(run,record); retention.checksums(run); print(run,flush=True)

if __name__=='__main__': main()
