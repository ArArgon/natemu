#!/usr/bin/env python3
"""Fresh retained build by default; --execute needs separate reviewed run budget."""
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
INPUTS = ('probe.c', 'guest.S', 'oracle.py', 'run.py', 'README.md', 'entitlements.plist')

def load(path, name):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--name',required=True)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    helper=REPO/'experiments/track01/source/run.py'
    retention=load(helper,'initial_retention')
    run=retention.allocate(os.environ.get('NATEMU_RAW_ROOT'),REPO,args.name)
    source=run/'source'; source.mkdir()
    for name in INPUTS: shutil.copyfile(HERE/name,source/name)
    shutil.copyfile(helper,source/'retention.py')
    for name in INPUTS:
        if (HERE/name).read_bytes() != (source/name).read_bytes():
            raise RuntimeError('input changed during snapshot: '+name)
    if helper.read_bytes() != (source/'retention.py').read_bytes():
        raise RuntimeError('retention helper changed during snapshot')
    retention=load(source/'retention.py','retained_retention')
    record=dict(schema='natemu.track03.pair-atomicity.v1', purpose='decision-evidence' if args.execute else 'build-only',
                status='preparing', commands=[],cells=[], execute_requested=args.execute,
                started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
                inherited_cpu_limit=list(resource.getrlimit(resource.RLIMIT_CPU)),
                vm_cpu_limit_setter='omitted',
                budget=dict(cells=3,vcpus=2,bytes_per_cell=0x24000,iterations_per_target=8000000,
                            per_cell_seconds=20,execution_seconds=100,build_jobs=2,build_command_seconds=60),
                inputs={name:hashlib.sha256((source/name).read_bytes()).hexdigest()
                        for name in INPUTS+('retention.py',)})
    try:
        retention.save(run,record)
        for cmd in (['sw_vers'],['uname','-a'],['sysctl','hw.model'],['sysctl','hw.cachelinesize'],['xcrun','clang','--version'],
                    ['xcrun','--show-sdk-version']):
            retention.command(run,record,cmd)
        sdk=retention.command(run,record,['xcrun','--show-sdk-path']).strip()
        retention.command(run,record,[sys.executable,'-B',source/'oracle.py','--self-check'])
        # Two jobs, no native worker execution in preparation. Frozen files unchanged.
        retention.command(run,record,['xcrun','clang','-arch','arm64','-c',source/'guest.S','-o',run/'guest.o'],timeout=60)
        retention.command(run,record,['xcrun','clang','-arch','arm64','-isysroot',sdk,'-std=c11',
            '-Wall','-Wextra','-Werror','-O2','-pthread',source/'probe.c',run/'guest.o',
            '-framework','Hypervisor','-o',run/'probe'],timeout=60)
        retention.command(run,record,['xcrun','otool','-tvV',run/'guest.o'])
        retention.command(run,record,['xcrun','otool','-s','__TEXT','__text',run/'guest.o'])
        retention.command(run,record,['xcrun','nm','-n',run/'probe'])
        if args.execute:
            if resource.getrlimit(resource.RLIMIT_CPU) != (resource.RLIM_INFINITY,resource.RLIM_INFINITY):
                raise RuntimeError('VM inherited CPU limit must be unlimited')
            retention.command(run,record,['codesign','--force','--sign','-','--entitlements',source/'entitlements.plist',run/'probe'])
            retention.command(run,record,['codesign','--verify','--verbose=2',run/'probe'])
            retention.command(run,record,['codesign','-d','--entitlements',':-',run/'probe'])
            oracle=load(source/'oracle.py','retained_oracle')
            deadline=time.monotonic()+100
            for mode, writer in (('control', 0), ('paired', 0), ('paired', 1)):
                cell=dict(mode=mode,writer=writer,status='running',command_index=len(record['commands']))
                record['cells'].append(cell); retention.save(run,record)
                remaining=deadline-time.monotonic()
                if remaining<=0: raise TimeoutError('overall execution ceiling')
                raw=retention.command(run,record,[run/'probe',mode,str(writer)],timeout=min(20,remaining),cpu_limit=False)
                rows=[json.loads(line) for line in raw.splitlines()]
                if rows[0].get('mode')!=mode or rows[0].get('writer')!=writer:
                    raise ValueError('cell identity mismatch')
                cell['native_returncode']=0; cell['timed_out']=False
                cell['oracle']=oracle.check(rows); cell['status']='completed'
                retention.save(run,record)
                expected='torn_control_detected' if mode=='control' else 'finite_no_tearing_observed'
                if cell['oracle']['status']!=expected:
                    record['status']='stopped_requires_findings_review'
                    return
        record['status']='completed_three_cells_requires_findings_review' if args.execute else 'built_not_executed'
    except BaseException as error:
        record['status']='failed'; record['error']=type(error).__name__+': '+str(error)
        if record['cells'] and record['cells'][-1]['status']=='running':
            cell=record['cells'][-1]; cell['status']='failed'; cell['error']=record['error']
            if cell['command_index'] < len(record['commands']):
                command=record['commands'][cell['command_index']]
                cell['native_returncode']=command.get('returncode')
                cell['timed_out']=command.get('status')=='timeout'
        raise
    finally:
        record['finished_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        retention.save(run,record); retention.checksums(run)
        print(run)

if __name__=='__main__': main()
