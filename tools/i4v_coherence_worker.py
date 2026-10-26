#!/usr/bin/env python3
"""Compile the owner-bound load/store used by native coherence profiles."""
import argparse
import json
from pathlib import Path
import struct

from i4v_address_worker import compiler_slot
from i4v_producer.compile import compile_source
from i4v_producer.export import CASES, bounded, export_program, require, verify

MISS = 0x493456a1
DELIVER = 0x493456a2


def request(vm, n, profile, actor, owner, kind):
    require(all(type(v) is int for v in (vm, n, profile, actor, owner, kind)),
            'integer coherence identities required')
    require(0 < vm < 1 << 64 and 1 <= n <= 4 and 0 <= profile < 6
            and 0 <= actor < n and 0 <= owner < n and kind in (0, 1),
            'unsupported coherence identity')
    bits = 64 if profile in (0, 3, 5) else 32
    source = 0x0389 if kind == 0 else 0x038b
    context = (profile + 1) | (n << 8) | (actor << 16)
    return (MISS, 1, vm, owner, 1, kind + 1, context,
            0x102000 + owner * 256 + kind * 16, bits, source, 2, 8, kind, 1, 0, 0)


def parse_request(raw):
    require(len(raw) == 128, 'coherence request must contain sixteen words')
    packet = struct.unpack('<16Q', raw)
    context = packet[6]
    expected = request(packet[2], (context >> 8) & 255, (context & 255) - 1,
                       (context >> 16) & 255, packet[3], packet[12])
    require(packet == expected, 'coherence owner/profile/source/decoding mismatch')
    return packet


def case_of(packet):
    return ('fex_store_eax_' if packet[12] == 0 else 'fex_load_eax_') + (
        'rbx64' if packet[8] == 64 else 'ebx32')


def make_reply(packet, body):
    parse_request(struct.pack('<16Q', *packet))
    require(body == bytes.fromhex(CASES[case_of(packet)]['body']) and len(body) == 8,
            'coherence body differs from admitted generated instructions')
    result = list(packet)
    result[0] = DELIVER
    result[14] = int.from_bytes(body, 'little')
    return struct.pack('<16Q', *result)


def verify_request(directory, expected):
    directory = Path(directory)
    packet = parse_request(bounded(directory / 'request.bin', 128))
    require(list(packet) == list(expected), 'coherence compilation differs from resident request')
    require(directory.parent.name == f'cpu-{packet[3]}'
            and directory.name == f'request-{packet[12]:03}',
            'coherence owner or compilation order differs')
    case = case_of(packet)
    spec = CASES[case]
    program = verify(directory / 'compile', dict(case=case, guest_rip=packet[7],
                     bitness=packet[8], source_hex=spec['source']))
    raw = make_reply(packet, bytes.fromhex(program['body_hex']))
    require(bounded(directory / 'reply.bin', 128) == raw,
            'coherence reply differs from actual compilation')
    record = dict(schema='natemu-i4v-coherence-worker-v1', request=list(packet),
                  reply=list(struct.unpack('<16Q', raw)),
                  producer_sha256=program['producer_sha256'], body_sha256=program['body_sha256'])
    require(json.loads(bounded(directory / 'worker.json', 8192)) == record,
            'coherence worker record differs')
    return program


def compile_request(directory, producer):
    directory = Path(directory).resolve()
    packet = parse_request(bounded(directory / 'request.bin', 128))
    require(directory.parent.name == f'cpu-{packet[3]}'
            and directory.name == f'request-{packet[12]:03}',
            'coherence compilation owner/order differs')
    case = case_of(packet)
    spec = CASES[case]
    with compiler_slot(directory.parent.parent):
        compile_source(directory / 'compile', Path(producer), bytes.fromhex(spec['source']),
                       packet[7], packet[8], 1, spec['host_count'])
    export_program(directory / 'compile', case)
    program = verify(directory / 'compile', dict(case=case, guest_rip=packet[7],
                     bitness=packet[8], source_hex=spec['source']))
    raw = make_reply(packet, bytes.fromhex(program['body_hex']))
    with (directory / 'reply.bin').open('xb') as stream:
        stream.write(raw)
    record = dict(schema='natemu-i4v-coherence-worker-v1', request=packet,
                  reply=struct.unpack('<16Q', raw),
                  producer_sha256=program['producer_sha256'], body_sha256=program['body_sha256'])
    with (directory / 'worker.json').open('x') as stream:
        json.dump(record, stream, indent=2)
        stream.write('\n')
    verify_request(directory, packet)


def verify_compilations(root, requests, replies):
    directories = sorted(Path(root).glob('request-*'))
    require(len(directories) == len(requests) == len(replies) == 2,
            'one store and one load compilation per owner required')
    exports = []
    for directory, packet, reply in zip(directories, requests, replies):
        exports.append(verify_request(directory, packet))
        raw = bounded(directory / 'reply.bin', 128)
        require(raw == struct.pack('<16Q', *reply)
                and bounded(directory / 'reply-delivered.bin', 128) == raw,
                'coherence delivered body/identity differs')
    return exports


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request-dir', required=True, type=Path)
    parser.add_argument('--producer', required=True, type=Path)
    args = parser.parse_args()
    compile_request(args.request_dir, args.producer)
