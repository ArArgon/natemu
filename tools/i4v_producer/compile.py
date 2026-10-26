#!/usr/bin/env python3
"""Retain one bounded, compile-only FEX request at its actual guest RIP."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import time
import uuid

IMAGE = 'sha256:27475cb39ecb4500d02b7c6e73c9d598e8d9f0736f0dc217d2d985e5cbe01aac'

def compile_source(directory, producer, source, guest_rip, bitness, instructions, expected_host=0):
    assert bitness in (32, 64) and 0 < len(source) <= 64 and 0 < instructions <= 32
    assert 0 <= guest_rip <= (1 << bitness) - 1 - len(source)
    assert bitness == 32 or guest_rip >> 47 in (0, 0x1ffff)
    directory, producer = Path(directory).resolve(), Path(producer).resolve()
    pin_path=Path(__file__).with_name('producer.json')
    if pin_path.exists():
        pin=json.loads(pin_path.read_text())
        if hashlib.sha256(producer.read_bytes()).hexdigest()!=pin['binary_sha256']:
            raise ValueError('producer differs from pinned I4-V build')
    directory.mkdir()
    (directory/'outputs').mkdir(); (directory/'empty-home').mkdir()
    raw = struct.pack('<6Q', bitness, 25, 1, 8192, 49155, 0)
    raw += struct.pack('<128sqQQI', b'natemu-i4v-request', expected_host, len(source), instructions, 0x41424344) + source
    (directory/'input.bin').write_bytes(raw)
    (directory/'source.x86').write_bytes(source)
    record = dict(schema='natemu-i4v-compile-v1', guest_rip=guest_rip, bitness=bitness,
                  source_hex=source.hex(), instructions=instructions, expected_host=expected_host,
                  image=IMAGE, producer_sha256=hashlib.sha256(producer.read_bytes()).hexdigest(), commands=[], status='running')
    def save(): (directory/'compiler.json').write_text(json.dumps(record, indent=2)+'\n')
    def run(argv, timeout=30, allowed=(0,)):
        item = dict(argv=argv, timeout_seconds=timeout); record['commands'].append(item); save()
        start=time.monotonic()
        result=subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        item.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr, elapsed_seconds=time.monotonic()-start); save()
        assert result.returncode in allowed, (argv[:2], result.returncode, result.stdout, result.stderr)
        return result
    container='natemu-i4v-'+uuid.uuid4().hex
    try:
        run(['docker','create','--name',container,'--cpus=2','--memory=512m','--memory-swap=512m','--pids-limit=128',
             '--network=none','--read-only','--tmpfs','/tmp:rw,size=64m','--tmpfs','/var/lib/postgresql/data:rw,size=1m',
             '--mount',f'type=bind,src={directory},dst=/work','--mount',f'type=bind,src={producer},dst=/producer,readonly',
             '--entrypoint=/usr/bin/env',IMAGE,'-i','PATH=/usr/bin:/bin','HOME=/work/empty-home',
             'NATEMU_EXPORT=/work/outputs',f'NATEMU_GUEST_RIP={guest_rip}',
             'timeout','30','/producer','/work/input.bin'])
        result=run(['docker','start','--attach',container],40,allowed=(0,1))
        log=result.stdout+result.stderr; (directory/'producer.log').write_text(log)
        state=json.loads(run(['docker','inspect',container]).stdout)[0]['State']; record['container_state']=state
        assert not state['Running'] and not state['OOMKilled'] and state['ExitCode']==result.returncode
        assert f'NATEMU_COMPLETED tests=1 result={result.returncode}' in log
        if 'PartialDecode' in log:
            raise ValueError('source window ends inside an instruction')
        # A probe may discover instruction counts; retain that status separately.
        if result.returncode:
            assert expected_host == 0 and 'Fail: Test took ' in log
            assert all(any(token in line for token in ('Fail: \'natemu-i4v-request\'', 'Fail: Test took ', 'Fail: Excuse me', 'Fail: Please bump'))
                       for line in log.splitlines() if 'Fail:' in line), log
        assert f'NATEMU_MODE bitness={bitness}' in log
        assert f'NATEMU_WINDOW index=1 guest_rip={guest_rip} source_address=65536 bytes={len(source)} instructions={instructions}' in log
        assert f'NATEMU_ENTRY index=1 guest_rip={guest_rip}' in log
        objects=[json.loads(line[len('NATEMU_OBJECT '):]) for line in log.splitlines() if line.startswith('NATEMU_OBJECT ')]
        assert len(objects)==2 and [o['index'] for o in objects]==[0,1]
        obj=objects[1]
        assert obj['guest_instructions']==instructions and 0 < obj['bytes'] <= 65536
        assert (directory/'outputs/001.native').stat().st_size==obj['bytes']
        assert (directory/'outputs/001.relocations').stat().st_size==obj['relocation_bytes']
        pc=[dict(zip(('guest_offset','native_offset'),map(int,m))) for m in re.findall(r'^NATEMU_GUEST_PC index=1 guest_offset=(\d+) native_offset=(\d+)$',log,re.M)]
        assert pc and all(0 <= p['guest_offset'] < len(source) and 0 <= p['native_offset'] < obj['bytes'] for p in pc)
        record.update(status='exported_not_executed', instruction_count_check=result.returncode, object=obj, guest_pc=pc)
    except BaseException as error:
        record.update(status='failed', error=str(error)); raise
    finally:
        try: run(['docker','rm','--force','--volumes',container])
        except BaseException:
            record['status']='cleanup_failed'; raise
        finally: save()
    return record

if __name__=='__main__':
    from export import CASES, export_program
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',required=True);p.add_argument('--producer',required=True)
    p.add_argument('--source');p.add_argument('--rip',type=lambda s:int(s,0),required=True)
    p.add_argument('--bitness',type=int,choices=(32,64),default=64);p.add_argument('--instructions',type=int)
    p.add_argument('--case',choices=CASES)
    p.add_argument('--expected-host',type=int,default=0)
    a=p.parse_args()
    if a.case:
        spec=CASES[a.case]
        if a.source is not None and a.source!=spec['source']: p.error('source does not match case')
        if a.instructions is not None and a.instructions!=spec['instructions']: p.error('instruction count does not match case')
        a.source,a.instructions,a.expected_host=spec['source'],spec['instructions'],spec['host_count']
    if a.source is None or a.instructions is None: p.error('provide --case or --source and --instructions')
    record=compile_source(a.directory,a.producer,bytes.fromhex(a.source),a.rip,a.bitness,a.instructions,a.expected_host)
    if a.case: record=export_program(a.directory,a.case)
    print(json.dumps(record,indent=2))
