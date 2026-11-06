#!/usr/bin/env python3
"""Retained, bounded candidate build and compile-only export."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.request
import uuid

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PINS = {
    'box64': ('ptitSeb/box64', '454d5387571bb5223fe8b43a4624debde8b74647'),
    'fex': ('FEX-Emu/FEX', '48d71752e2a363d5535181f803b0b0e9666c1825'),
    'tcg': ('qemu/qemu', 'c1c18d1e640b64292859ce9f30f3c344edfb0294'),
}


def load(path):
    spec = importlib.util.spec_from_file_location('retention', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', required=True)
    parser.add_argument('--candidate', choices=PINS, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--seconds', type=int, default=2400)
    parser.add_argument('--fetch-only', action='store_true')
    parser.add_argument('--source-archive')
    parser.add_argument('--fixtures', type=Path, help='six retained comparison fixtures; default is the original checkpoint set')
    parser.add_argument('--dependencies')
    parser.add_argument('--hardware-tso', action='store_true',
                        help='export a distinct hardware-TSO-dependent lowering profile')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 2700:
        parser.error('seconds must fit the 45-minute candidate allocation')
    helper = REPO/'experiments/track01/source/run.py'
    retention = load(helper)
    run = retention.allocate(os.environ.get('NATEMU_RAW_ROOT'), REPO, args.name)
    source = run/'source'; source.mkdir()
    fixtures = args.fixtures or HERE.parent/'codegen-checkpoint/fixtures.json'
    specification = json.loads(fixtures.read_text())
    if len(specification['Instructions']) != 6:
        raise ValueError('comparison requires exactly six named fixtures')
    for label, fixture in specification['Instructions'].items():
        if not label.replace('_', '').isalnum() or not label.isascii() or label == 'nop':
            raise ValueError('invalid fixture label')
        code = bytes.fromhex(fixture['ExpectedBytes'])
        if not 1 <= len(fixture['x86Insts']) <= 5 or not 1 <= len(code) <= 32:
            raise ValueError('fixture exceeds the retained exporter envelope')
    inputs = [HERE/'run.py', HERE/'make-fixtures.py', fixtures, helper]
    if not args.fetch_only:
        inputs += [HERE/(args.candidate+'.sh'), HERE/(args.candidate+'-export.py'),
                   HERE/'ordering-profile.py']
    for path in inputs:
        target = source/('retention.py' if path == helper else 'fixtures.json' if path == fixtures else path.name)
        shutil.copyfile(path, target)
        if path.read_bytes() != target.read_bytes():
            raise RuntimeError('source changed during snapshot')
    if args.dependencies:
        dependency_root = Path(args.dependencies).resolve()
        expected = {line.split('  ',1)[1]:line.split('  ',1)[0]
                    for line in (dependency_root/'SHA256SUMS').read_text().splitlines()}
        for name in ('dependencies.json','dependency-overlay.tar'):
            original=dependency_root/'source'/name
            if hashlib.sha256(original.read_bytes()).hexdigest() != expected['source/'+name]:
                raise ValueError('dependency snapshot checksum mismatch')
            shutil.copyfile(original,source/name)
    retention = load(source/'retention.py')
    repo, pin = PINS[args.candidate]
    record = dict(schema='natemu.track03.codegen-export.v2', purpose='exploratory',
                  candidate=args.candidate, pin=pin, image=args.image, status='preparing',
                  ordering_profile='hardware-tso-dependent' if args.hardware_tso else 'software-ordering',
                  commands=[], started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    deadline = time.monotonic()+args.seconds
    name = 'natemu-'+uuid.uuid4().hex
    created = False
    def command(argv, seconds=60):
        left = deadline-time.monotonic()
        if left <= 0: raise TimeoutError('candidate run budget exhausted')
        return retention.command(run, record, argv, timeout=min(left,seconds), cpu_limit=False)
    try:
        retention.save(run, record)
        record['source_url'] = 'https://codeload.github.com/'+repo+'/tar.gz/'+pin
        record['download_bytes'] = 0
        if args.source_archive:
            archive = Path(args.source_archive).resolve()
            origin = archive.parent.parent
            original = json.loads((origin/'manifest.json').read_text())
            expected = next(line.split()[0] for line in (origin/'SHA256SUMS').read_text().splitlines()
                            if line.endswith('  source/upstream.tar.gz'))
            if original['pin'] != pin or hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
                raise ValueError('retained source identity mismatch')
            shutil.copyfile(archive, source/'upstream.tar.gz')
            record['source_sha256'] = expected
            record['source_archive'] = str(archive)
            record['download_bytes'] = 0
        else:
            with urllib.request.urlopen(record['source_url'], timeout=30) as response, (source/'upstream.tar.gz').open('xb') as output:
                while True:
                    chunk = response.read(1024*1024)
                    if not chunk: break
                    record['download_bytes'] += len(chunk)
                    if record['download_bytes'] > 256*1024**2 or time.monotonic() >= deadline:
                        raise RuntimeError('source download exceeds byte/time budget')
                    output.write(chunk)
        retention.save(run, record)
        if args.fetch_only:
            record['status'] = 'source_retained_not_built'
            return
        command(['docker','image','inspect',args.image])
        created = True
        command(['docker','create','--name',name,'--cpus=2','--memory=8g','--memory-swap=8g',
                 '--pids-limit=512','--user=0','--tmpfs','/var/lib/postgresql/data',
                 '--env','NATEMU_HARDWARE_TSO='+str(int(args.hardware_tso)),
                 '--mount',f'type=bind,src={run},dst=/work','--entrypoint=/bin/sh',
                 args.image,'/work/source/'+args.candidate+'.sh'])
        command(['docker','start','--attach',name],args.seconds)
        state = json.loads(command(['docker','inspect',name]))[0]['State']
        if state['Running'] or state['ExitCode'] or state['OOMKilled']:
            raise RuntimeError('candidate container failed: '+json.dumps(state))
        record['status'] = 'exported_not_executed'
    except BaseException as error:
        record['status']='failed'; record['error']=type(error).__name__+': '+str(error)
        raise
    finally:
        if created:
            try:
                retention.command(run,record,['docker','rm','--force','--volumes',name],timeout=30,cpu_limit=False)
            except Exception as error:
                record['cleanup_error']=str(error); record['status']='failed_cleanup'
        record['finished_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        retention.save(run,record); retention.checksums(run)
        print(run,flush=True)


if __name__=='__main__': main()
