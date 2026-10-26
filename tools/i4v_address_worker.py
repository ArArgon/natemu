#!/usr/bin/env python3
"""Compile one protected32 address instruction with its complete owner/fetch binding."""
import argparse
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import struct
import time

from i4v_producer.compile import compile_source
from i4v_producer.export import CASES, bounded, export_program, require, verify

MISS = 0x49345671
DELIVER = 0x49345672
EIP = 0x2000


@contextmanager
def compiler_slot(root, timeout=90):
    """Bound expensive compiler processes, independent of deferred reply delivery."""
    deadline = time.monotonic() + timeout
    slots = []
    try:
        for i in range(2):
            slots.append(open(Path(root) / f'compiler-slot-{i}.lock', 'a+b'))
        while True:
            for slot in slots:
                try:
                    fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    yield
                finally:
                    fcntl.flock(slot, fcntl.LOCK_UN)
                return
            if time.monotonic() >= deadline:
                raise TimeoutError('address compiler slots remained occupied')
            time.sleep(.01)
    finally:
        for slot in slots:
            slot.close()


def case_for(profile, generation, step):
    require(0 <= profile < 6 and generation in (1, 2) and 0 <= step < 3,
            'unsupported address instruction profile/generation/step')
    if step == 0:
        return 'fex_moffs_load32_step8'
    suffix = '16' if profile == 3 else ''
    if step == 1:
        return 'fex_address_store_' + ('ecx' if generation == 1 else 'edx') + suffix
    return 'fex_address_load16' if profile == 3 else 'fex_address_load_edx'


def request(vm, n, profile, actor, owner, generation, serial, terminal, step):
    require(type(vm) is int and 0 < vm < 1 << 64 and type(serial) is int and 0 < serial < 1 << 64,
            'invalid VM/serial identity')
    require(1 <= n <= 4 and 0 <= actor < n and 0 <= owner < n and terminal in (0, 1),
            'invalid configured owner/context')
    require(not terminal or (profile >= 4 and owner == actor), 'terminal context belongs to the faulting actor')
    case = case_for(profile, generation, step)
    spec = CASES[case]
    source = bytes.fromhex(spec['source'])
    offset = ((0, 2, 5) if profile == 3 else (0, 2, 4))[step]
    eip = EIP + offset
    if terminal and profile == 5 and owner == actor:
        require(eip + len(source) - 1 <= EIP + 2, 'terminal CS limit rejects instruction fetch')
    mode = int(owner != actor)
    context = (profile + 1) | (n << 8) | (actor << 16) | (terminal << 24) | (mode << 25)
    linear = (0x400123 if mode else 0x300123) + eip
    gpa = 0x300123 + eip
    if profile == 1:
        gpa &= ~(1 << 20)
    return (MISS, 1, vm, owner, 1, generation, context, eip, 32,
            int.from_bytes(source.ljust(8, b'\0'), 'little'), len(source),
            linear, gpa, serial, len(bytes.fromhex(spec['body'])), step)


def parse_request(raw):
    require(len(raw) == 128, 'address request must contain sixteen words')
    packet = struct.unpack('<16Q', raw)
    context = packet[6]
    profile, n, actor, terminal = (context & 255) - 1, (context >> 8) & 255, (context >> 16) & 255, (context >> 24) & 1
    expected = request(packet[2], n, profile, actor, packet[3], packet[5], packet[13], terminal, packet[15])
    require(packet == expected, 'address instruction owner/context/source/fetch mismatch')
    return packet


def make_reply(packet, body):
    parse_request(struct.pack('<16Q', *packet))
    profile = (packet[6] & 255) - 1
    spec = CASES[case_for(profile, packet[5], packet[15])]
    require(body == bytes.fromhex(spec['body']) and len(body) <= 16, 'address instruction body mismatch')
    reply = list(packet)
    reply[0] = DELIVER
    reply[9:11] = struct.unpack('<2Q', body.ljust(16, b'\0'))
    return struct.pack('<16Q', *reply)


def compile_request(directory, producer):
    directory = Path(directory).resolve()
    packet = parse_request(bounded(directory / 'request.bin', 128))
    case = case_for((packet[6] & 255) - 1, packet[5], packet[15])
    spec = CASES[case]
    require(directory.parent.name == f'cpu-{packet[3]}' and directory.name.startswith('request-'),
            'address compilation must use its configured owner directory')
    with compiler_slot(directory.parent.parent):
        compile_source(directory / 'compile', Path(producer), bytes.fromhex(spec['source']), packet[7], 32, 1, spec['host_count'])
    export_program(directory / 'compile', case)
    program = verify(directory / 'compile', dict(case=case, guest_rip=packet[7], bitness=32, source_hex=spec['source']))
    reply = make_reply(packet, bytes.fromhex(program['body_hex']))
    with (directory / 'reply.bin').open('xb') as stream:
        stream.write(reply)
    record = dict(schema='natemu-i4v-address-worker-v1', request=packet, reply=struct.unpack('<16Q', reply),
                  producer_sha256=program['producer_sha256'], body_sha256=program['body_sha256'])
    with (directory / 'worker.json').open('x') as stream:
        json.dump(record, stream, indent=2)
        stream.write('\n')
    verify_request(directory, packet)


def verify_request(directory, expected):
    directory = Path(directory)
    packet = parse_request(bounded(directory / 'request.bin', 128))
    require(list(packet) == list(expected), 'compiled instruction differs from resident address request')
    case = case_for((packet[6] & 255) - 1, packet[5], packet[15])
    spec = CASES[case]
    program = verify(directory / 'compile', dict(case=case, guest_rip=packet[7], bitness=32, source_hex=spec['source']))
    raw = make_reply(packet, bytes.fromhex(program['body_hex']))
    require(bounded(directory / 'reply.bin', 128) == raw, 'address reply differs from actual compilation')
    record = dict(schema='natemu-i4v-address-worker-v1', request=list(packet), reply=list(struct.unpack('<16Q', raw)),
                  producer_sha256=program['producer_sha256'], body_sha256=program['body_sha256'])
    require(json.loads(bounded(directory / 'worker.json', 8192)) == record, 'address worker record differs')
    return program


def verify_compilations(root, requests, replies):
    directories = sorted(Path(root).glob('request-*'))
    require(len(directories) == len(requests) == len(replies), 'address compilation/delivery count differs')
    for index, (directory, packet, reply) in enumerate(zip(directories, requests, replies)):
        require(directory.name == f'request-{index:03}', 'address compilation order differs')
        verify_request(directory, packet)
        expected = make_reply(packet, bytes.fromhex(CASES[case_for((packet[6] & 255) - 1, packet[5], packet[15])]['body']))
        require(struct.pack('<16Q', *reply) == expected, 'reported address delivery differs from compilation')
        require(bounded(directory / 'reply-delivered.bin', 128) == expected, 'actual address delivery differs from compilation')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request-dir', required=True, type=Path)
    parser.add_argument('--producer', required=True, type=Path)
    args = parser.parse_args()
    compile_request(args.request_dir, args.producer)
