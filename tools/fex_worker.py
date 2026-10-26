#!/usr/bin/env python3
"""Compile one finite I2 request with pinned host-side FEX; never execute guest code."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import time
import uuid
from i4v_producer.ownership import Container, atomic_json, cleanup_containers

IMAGE = 'sha256:27475cb39ecb4500d02b7c6e73c9d598e8d9f0736f0dc217d2d985e5cbe01aac'
PRODUCER_SHA = '35d187ac6a2498ced8afebd167f230357880d7b78fcfa6d0560fc2065fce9496'
XOR = bytes.fromhex('660fefc14801d1488b030f94c2f30f7f07')
OR = bytes.fromhex('660febc14801d1488b030f94c2f30f7f07')
TAIL = bytes.fromhex('fb0005cafa0005abe7031aaac4fcdfc81f2003d5f4179f9a851e40b3bf3b03d57001803d1f4000d5')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def parse_request(raw):
    require(len(raw) == 128, 'request must contain exactly sixteen words')
    p = struct.unpack('<16Q', raw)
    require(p[0] == 11 and p[1] == 3 and all(p[n] > 0 for n in (2, 3, 4, 5)), 'invalid request identity')
    require(p[6] == 17 and p[10:13] == (1, 0, 0x102000), 'unsupported source/profile/vCPU/RIP')
    padded = struct.pack('<3Q', *p[7:10])
    source = padded[:17]
    require(not any(padded[17:]) and source in (XOR, OR), 'unsupported source bytes/padding')
    return p, source


def extract(directory, log, source):
    require('NATEMU_COMPLETED tests=1 result=0' in log, 'compiler did not complete request')
    require('NATEMU_FEATURES sve128=0 sve256=0 rcpc=0 imm9=0' in log, 'wrong compiler feature profile')
    entries = [json.loads(line[len('NATEMU_OBJECT '):]) for line in log.splitlines() if line.startswith('NATEMU_OBJECT ')]
    require(len(entries) == 2 and [entry['index'] for entry in entries] == [0, 1], 'wrong export count')
    obj = entries[1]
    require((obj['bytes'], obj['code_offset'], obj['code_bytes'], obj['guest_instructions'], obj['relocation_bytes']) == (160, 4, 108, 5, 144), 'unsupported object layout')
    with (directory / 'outputs/001.native').open('rb') as stream:
        raw = stream.read(161)
    with (directory / 'outputs/001.relocations').open('rb') as stream:
        relocations = stream.read(145)
    require(len(raw) == 160 and len(relocations) == 144, 'truncated producer output')
    require(raw[:12].hex() == '70000000e0ffff10800300f9' and raw[56:60].hex() == '0f420000', 'unexpected prefix/suffix boundary')
    body = raw[12:56]
    first = bytes.fromhex('101e316e' if source == XOR else '101eb14e')
    require(body == first + TAIL, 'unsupported native instruction shape')
    return body, obj


def compile_request(directory, producer):
    directory, producer = directory.resolve(), producer.resolve()
    with (directory / 'request.bin').open('rb') as stream:
        p, source = parse_request(stream.read(129))
    require(producer.stat().st_size <= 16*1024**2, 'oversized producer')
    require(hashlib.sha256(producer.read_bytes()).hexdigest() == PRODUCER_SHA, 'producer differs from pinned build')
    (directory / 'outputs').mkdir()
    (directory / 'empty-home').mkdir()
    record = {'format': 'natemu-i2-compiler-v1', 'status': 'running', 'request': list(p), 'x86_hex': source.hex(),
              'producer_sha256': PRODUCER_SHA, 'image': IMAGE, 'commands': [],
              'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    def save():
        atomic_json(directory / 'compiler.json', record)
    def command(argv, timeout=10, output=None):
        item = {'argv': argv, 'timeout_seconds': timeout}
        record['commands'].append(item); save()
        start = time.monotonic()
        try:
            if output:
                with (directory / output).open('xb') as stream:
                    result = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
                text = ''
            else:
                result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
                text = result.stdout
                item['stdout'], item['stderr'] = text, result.stderr
            item['returncode'] = result.returncode
            require(result.returncode == 0, f'compiler transport failed: {argv[:2]} ({result.returncode})')
            return text
        finally:
            item['elapsed_seconds'] = time.monotonic()-start
            save()
    container = 'natemu-i2-'+uuid.uuid4().hex
    owned = Container(directory, container)
    try:
        (directory / 'input.x86').write_bytes(source)
        header = struct.pack('<6Q', 64, 25, 1, 8192, 49155, 0)
        entry = struct.pack('<128sqQQI', b'natemu-live-request', 11, len(source), 5, 0x41424344)
        (directory / 'input.bin').write_bytes(header+entry+source)
        owned.creating()
        created = command(['docker','create','--name',container,'--label',owned.label,'--label',f'natemu.i2.run={os.environ.get("NATEMU_I2_RUN", "manual")}', '--cpus=2','--memory=512m','--memory-swap=512m','--pids-limit=128',
                 '--network=none','--read-only','--tmpfs','/tmp:rw,size=64m','--tmpfs','/var/lib/postgresql/data:rw,size=1m',
                 '--mount',f'type=bind,src={directory},dst=/work',
                 '--mount',f'type=bind,src={producer},dst=/producer,readonly',
                 '--entrypoint=/usr/bin/env',IMAGE,'-i','PATH=/usr/bin:/bin','HOME=/work/empty-home',
                 'NATEMU_EXPORT=/work/outputs','timeout','30','/producer','/work/input.bin','/work/normalized.json'])
        owned.created(created)
        command(['docker','start','--attach',container],timeout=40,output='producer.log')
        state = json.loads(command(['docker','inspect',container]))[0]['State']
        record['container_state'] = state
        require(not state['Running'] and not state['OOMKilled'] and state['ExitCode'] == 0, 'compiler container did not succeed')
        body, obj = extract(directory, (directory / 'producer.log').read_text(), source)
        reply = [2, 3, *p[2:6], 44, *struct.unpack('<6Q', body+bytes(4)), *p[7:10]]
        require(len(reply) == 16, 'internal reply size')
        with (directory / 'reply.bin').open('xb') as out:
            out.write(struct.pack('<16Q', *reply))
        (directory / 'body.arm64').write_bytes(body)
        record.update(status='passed', object=obj, native_sha256=hashlib.sha256(body).hexdigest(), native_hex=body.hex(),
                      extraction=[12,56], reply=reply)
    except BaseException as error:
        record.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        try:
            identity=owned.record.get('container_id')
            if identity is not None:
                command(['docker','rm','--force','--volumes',identity])
                owned.removed()
            else:
                record['cleanup']=cleanup_containers(owned.registry)
                require(record['cleanup']['complete'],'unacknowledged owned container cleanup')
        except Exception as error:
            record['cleanup_error'] = str(error)
            record['status'] = 'failed'
        record['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        save()
    require(record['status'] == 'passed', 'compiler cleanup failed')


def verify_compilations(root, requests):
    """Cross-check actual worker files against the resident requests in the trace."""
    directories=sorted(root.glob('request-*'))
    require(len(directories)==len(requests), 'worker invocation count differs from runtime misses')
    for index,(directory,expected) in enumerate(zip(directories,requests)):
        require(directory.name==f'request-{index:03}', 'missing/reordered compiler directory')
        p,source=parse_request((directory/'request.bin').read_bytes())
        require(list(p)==expected, 'worker input is not the runtime request')
        require((directory/'input.x86').read_bytes()==source, 'FEX input bytes differ')
        packed=struct.pack('<6Q',64,25,1,8192,49155,0)+struct.pack('<128sqQQI',b'natemu-live-request',11,17,5,0x41424344)+source
        require((directory/'input.bin').read_bytes()==packed, 'FEX packed input differs')
        body,obj=extract(directory,(directory/'producer.log').read_text(),source)
        record=json.loads((directory/'compiler.json').read_text())
        require(record['status']=='passed' and record['producer_sha256']==PRODUCER_SHA and record['image']==IMAGE, 'wrong producer or failed invocation')
        require(record['request']==expected and record['object']==obj and record['native_sha256']==hashlib.sha256(body).hexdigest(), 'producer record does not bind output')
        require(all(c.get('returncode')==0 for c in record['commands']) and len(record['commands'])==4, 'incomplete compiler command lifecycle')
        reply=[2,3,*p[2:6],44,*struct.unpack('<6Q',body+bytes(4)),*p[7:10]]
        require(record['reply']==reply and (directory/'reply.bin').read_bytes()==struct.pack('<16Q',*reply), 'reply is not fresh compiler output')
        require((directory/'body.arm64').read_bytes()==body, 'installed body differs from extracted output')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request-dir', required=True, type=Path)
    parser.add_argument('--producer', required=True, type=Path)
    args = parser.parse_args()
    compile_request(args.request_dir, args.producer)
