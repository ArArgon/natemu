#!/usr/bin/env python3
"""Compile one I3 request with pinned FEX and export its allocated recovery maps.

This is a compile-only worker. The 1168-byte wire record admits exactly the two
finite profiles in i3_producer; it has no generic carry-recipe or access-size ABI.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import time
import uuid

from i3_producer import ALIAS, FP_ALIAS, ROOTS, make_input, parse

IMAGE = 'sha256:27475cb39ecb4500d02b7c6e73c9d598e8d9f0736f0dc217d2d985e5cbe01aac'
PRODUCER_SHA = 'cef6a909ad1427018f33c2896a97975e2244e4ba776439d661cd7f75a83f6fe2'
EXPORT_BYTES = 1168


def require(condition, message):
    if not condition:
        raise ValueError(message)


def bounded(path, limit):
    with path.open('rb') as stream:
        result = stream.read(limit + 1)
    require(len(result) <= limit, f'oversized artifact: {path.name}')
    return result


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def parse_request(raw):
    require(len(raw) == 128, 'request must contain exactly sixteen words')
    p = struct.unpack('<16Q', raw)
    require(p[:2] == (11, 3) and all(p[n] > 0 for n in (2, 3, 4, 5)), 'invalid request identity')
    require(p[6] in (16, 17) and p[10:13] == (2, 0, 0x102000), 'unsupported source/profile/vCPU/RIP')
    padded = struct.pack('<3Q', *p[7:10])
    source = padded[:p[6]]
    require(not any(padded[p[6]:]) and source in (ALIAS, FP_ALIAS), 'unsupported source bytes/padding')
    return p, source


def serialize_export(maps, source):
    """Serialize validated maps, refusing semantics absent from this wire ABI."""
    require(source in (ALIAS, FP_ALIAS) and maps['source_hex'] == source.hex(), 'map/source mismatch')
    body, spill = maps['body'], maps['spill_bytes']
    require(0 < len(body) <= 512 and len(body) % 4 == 0, 'body outside wire capacity')
    require(0 < spill <= 2048 and spill % 32 == 0, 'spill frame outside admitted capacity')
    require(maps['stack_restored_by_body'] and maps['root_order'] == ROOTS, 'wrong stack/root contract')
    sites = maps['sites']
    require(len(sites) == 2, 'wire requires exactly two memory sites')
    out = bytearray(struct.pack('<3Q', len(body), len(source), spill))
    out += source.ljust(24, b'\0') + body.ljust(512, b'\0')
    for index, site in enumerate(sites):
        require((site['access'], site['bytes']) == (('load', 8), ('store', 16))[index], 'wrong access profile')
        require(site['carry_inverted'] is False and site['fpsr_authority'] == 'captured-native-FPSR', 'unrepresentable flag authority')
        require(site['native_fp_prefix'] == (source == FP_ALIAS) and site['phase'] == 'precommit', 'wrong recovery phase')
        offset, guest = site['native_offset'], site['guest_offset']
        require(0 <= offset < len(body) and offset % 4 == 0 and site['native_continuation'] == offset + 4,
                'invalid access/continuation')
        require(guest == ((7, 13) if source == ALIAS else (6, 12))[index], 'wrong guest instruction boundary')
        require([root['role'] for root in site['roots']] == ROOTS, 'wrong allocated root order')
        out += struct.pack('<3Q', offset, guest, index)
        for root in site['roots']:
            vector = root['role'].startswith('XMM')
            require(root['bytes'] == (16 if vector else 8), 'wrong root width')
            kind = root['kind']
            if kind == 'spill':
                location = root['offset']
                require(not vector and location % 32 == 0 and 0 <= location <= spill - 8, 'invalid spill root')
                require(0 <= root['initializing_native_offset'] < offset, 'uninitialized spill root')
                tag = 3
            else:
                require(kind == ('vector' if vector else 'gpr'), 'wrong register class')
                location = root['register']
                require(0 <= location < (32 if vector else 31), 'invalid allocated register')
                tag = 2 if vector else 1
            out += struct.pack('<Q', tag | location << 8)
    require(len(out) == EXPORT_BYTES, 'internal export length')
    return bytes(out)


def json_maps(maps):
    return {key: value for key, value in maps.items() if key != 'body'} | {'body_hex': maps['body'].hex()}


def extract(directory, source):
    log = bounded(directory / 'producer.log', 1024**2).decode()
    objects = [json.loads(line.removeprefix('NATEMU_OBJECT ')) for line in log.splitlines()
               if line.startswith('NATEMU_OBJECT ')]
    require(len(objects) == 2 and [obj['index'] for obj in objects] == [0, 1], 'not a fresh single-request compile')
    require('NATEMU_COMPLETED tests=1 result=0' in log.splitlines(), 'compiler did not complete successfully')
    require(sorted(path.name for path in (directory / 'outputs').iterdir()) ==
            ['000.native', '000.relocations', '001.native', '001.relocations'], 'unexpected producer outputs')
    for index, obj in enumerate(objects):
        require(len(bounded(directory / f'outputs/{index:03}.native', 65536)) == obj['bytes'], 'raw object size mismatch')
        require(len(bounded(directory / f'outputs/{index:03}.relocations', 65536)) == obj['relocation_bytes'], 'relocation size mismatch')
    maps = parse(log, bounded(directory / 'outputs/001.native', 65536), 1, source)
    require(maps['count_tool_status'] == 0, 'count-tool failure')
    return maps


def commands(directory, producer, container, run_label):
    return [
        (['docker', 'create', '--name', container, '--label', f'natemu.i3.run={run_label}',
          '--cpus=2', '--memory=512m', '--memory-swap=512m', '--pids-limit=128',
          '--network=none', '--read-only', '--tmpfs', '/tmp:rw,size=64m',
          '--tmpfs', '/var/lib/postgresql/data:rw,size=1m',
          '--mount', f'type=bind,src={directory},dst=/work',
          '--mount', f'type=bind,src={producer},dst=/producer,readonly',
          '--entrypoint=/usr/bin/env', IMAGE, '-i', 'PATH=/usr/bin:/bin', 'HOME=/work/empty-home',
          'NATEMU_CHECKPOINT_ROOT_PREP=1', 'NATEMU_EXPORT=/work/outputs',
          'timeout', '30', '/producer', '/work/input.bin', '/work/normalized.json'], 10),
        (['docker', 'start', '--attach', container], 40),
        (['docker', 'inspect', container], 10),
        (['docker', 'rm', '--force', '--volumes', container], 10),
    ]


ARTIFACTS = ['request.bin', 'input.x86', 'input.bin', 'producer.log', 'normalized.json',
             'outputs/000.native', 'outputs/000.relocations', 'outputs/001.native', 'outputs/001.relocations',
             'body.arm64', 'maps.json', 'export.bin', 'reply-request.bin']


def hashes(directory):
    return {name: sha(bounded(directory / name, 1024**2)) for name in ARTIFACTS}


def compile_request(directory, producer):
    directory, producer = directory.resolve(), producer.resolve()
    request = bounded(directory / 'request.bin', 128)
    p, source = parse_request(request)
    require(sha(bounded(producer, 16 * 1024**2)) == PRODUCER_SHA, 'producer differs from pinned build')
    (directory / 'outputs').mkdir()
    (directory / 'empty-home').mkdir()
    container = 'natemu-i3-' + uuid.uuid4().hex
    run_label = os.environ.get('NATEMU_I3_RUN', 'manual')
    plan = commands(directory, producer, container, run_label)
    record = {'format': 'natemu-i3-compiler-v1', 'status': 'running', 'request': list(p),
              'producer': str(producer), 'producer_sha256': PRODUCER_SHA, 'image': IMAGE,
              'container': container, 'run_label': run_label, 'commands': [],
              'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}

    def save():
        (directory / 'compiler.json').write_text(json.dumps(record, indent=2) + '\n')

    def command(index):
        argv, timeout = plan[index]
        item = {'argv': argv, 'timeout_seconds': timeout}
        record['commands'].append(item)
        save()
        start = time.monotonic()
        try:
            if index == 1:
                with (directory / 'producer.log').open('xb') as stream:
                    result = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
                output = ''
            else:
                result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
                output = result.stdout
                item.update(stdout=output, stderr=result.stderr)
            item['returncode'] = result.returncode
            require(result.returncode == 0, f'compiler transport failed: {argv[:2]} ({result.returncode})')
            return output
        finally:
            item['elapsed_seconds'] = time.monotonic() - start
            save()

    try:
        (directory / 'input.x86').write_bytes(source)
        (directory / 'input.bin').write_bytes(make_input(source))
        command(0)
        command(1)
        state = json.loads(command(2))[0]['State']
        record['container_state'] = state
        require(not state['Running'] and not state['OOMKilled'] and state['ExitCode'] == 0, 'compiler container did not succeed')
        maps = extract(directory, source)
        exported = serialize_export(maps, source)
        (directory / 'body.arm64').write_bytes(maps['body'])
        (directory / 'maps.json').write_text(json.dumps(json_maps(maps), indent=2) + '\n')
        with (directory / 'export.bin').open('xb') as stream:
            stream.write(exported)
        with (directory / 'reply-request.bin').open('xb') as stream:
            stream.write(request)
        record.update(status='passed', artifact_sha256=hashes(directory))
    except BaseException as error:
        record.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        try:
            command(3)
        except Exception as error:
            record.update(status='failed', cleanup_error=str(error))
        record['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        save()
    require(record['status'] == 'passed', 'compiler cleanup failed')


def verify_compilations(root, requests):
    """Reparse actual emitted bytes/maps and bind all artifacts to runtime misses."""
    directories = sorted(Path(root).glob('request-*'))
    require(len(directories) == len(requests), 'worker invocation count differs from runtime misses')
    for index, (directory, expected) in enumerate(zip(directories, requests)):
        directory = directory.resolve()
        require(directory.name == f'request-{index:03}', 'missing/reordered compiler directory')
        request = bounded(directory / 'request.bin', 128)
        p, source = parse_request(request)
        require(list(p) == expected, 'worker input is not the runtime request')
        require(bounded(directory / 'input.x86', 24) == source, 'FEX input bytes differ')
        require(bounded(directory / 'input.bin', 1024) == make_input(source), 'FEX packed input differs')
        maps = extract(directory, source)
        require(bounded(directory / 'export.bin', EXPORT_BYTES) == serialize_export(maps, source), 'export differs from actual allocated maps')
        require(bounded(directory / 'reply-request.bin', 128) == request, 'reply request identity differs')
        require(bounded(directory / 'body.arm64', 512) == maps['body'], 'installed body differs')
        require(json.loads(bounded(directory / 'maps.json', 65536)) == json_maps(maps), 'retained maps differ')
        record = json.loads(bounded(directory / 'compiler.json', 1024**2))
        require(record['format'] == 'natemu-i3-compiler-v1' and record['status'] == 'passed', 'failed/wrong worker record')
        require(record['request'] == expected and record['producer_sha256'] == PRODUCER_SHA and record['image'] == IMAGE, 'wrong producer/request binding')
        require(record['artifact_sha256'] == hashes(directory), 'retained artifact checksum differs')
        require(re.fullmatch(r'natemu-i3-[0-9a-f]{32}', record['container']) is not None, 'invalid container identity')
        producer = Path(record['producer'])
        require(producer.is_absolute() and sha(bounded(producer, 16 * 1024**2)) == PRODUCER_SHA, 'retained producer differs')
        plan = commands(directory, producer, record['container'], record['run_label'])
        actual = record['commands']
        require(len(actual) == len(plan), 'incomplete compiler lifecycle')
        for item, (argv, timeout) in zip(actual, plan):
            require(item['argv'] == argv and item['timeout_seconds'] == timeout and item.get('returncode') == 0,
                    'wrong command lifecycle or isolation limits')
            require(0 <= item['elapsed_seconds'] <= timeout + 5, 'invalid command duration')
        state = json.loads(actual[2]['stdout'])[0]['State']
        require(state == record['container_state'] and not state['Running'] and not state['OOMKilled'] and state['ExitCode'] == 0,
                'compiler lifecycle did not finish successfully')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request-dir', required=True, type=Path)
    parser.add_argument('--producer', required=True, type=Path)
    args = parser.parse_args()
    compile_request(args.request_dir, args.producer)
