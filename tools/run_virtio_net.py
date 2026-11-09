#!/usr/bin/env python3
"""Bounded N=1 network frontend checks through ordinary virtio-MMIO.

The independent owned-packet endpoint echoes each TX once, with no native network
or guest-memory access. These are frontend development checks, not native backend
acceptance or performance evidence. No builds, retries, privileges or network calls.
"""
import argparse
import copy
import fcntl
import hashlib
import json
import pathlib
import struct
import subprocess
import sys
import tempfile
import time

from run_io_windows import ROOT, digest, stop_group
sys.path.insert(0, str(ROOT / 'tools/reference'))
import ref
from io_common import VM_LOCK

BASE, CODE, STACK, MMIO = 0x400000, 0x100000, 0x300000, 0xf0001000
SIZE = 0x60000
CASES = ('headers', 'short-rx', 'queues', 'malformed', 'halt-irq', 'active-other-queue', 'active-power')


def frame(index, size):
    payload = b''.join(hashlib.sha256(f'net-frontend-v1:{index}:{i}'.encode()).digest()
                       for i in range((size + 31) // 32))
    header = bytes.fromhex('020000000002020000000001' + ('8100000788b5' if size == 1518 else '88b5'))
    return header + payload[:size - len(header)]


def fixture(case, work):
    memory, expected = bytearray(SIZE), None
    assembly, packets, completions, modifications = [], [], [], []
    extra = []
    arrival = None
    check = 0
    def put(address, fmt, *values): struct.pack_into(fmt, memory, address - BASE, *values)
    def store(address, data): memory[address - BASE:address - BASE + len(data)] = data
    def emit(text): assembly.append(text)
    def write(offset, value): emit(f'mov dword ptr [r12 + {offset:#x}], {value}')
    def read(offset): emit(f'mov eax, dword ptr [r12 + {offset:#x}]')
    def checkreg(value):
        nonlocal check
        check += 1; emit(f'mov r15, {check}\ncmp eax, {value}\njne failed')
    def checkmem(address, value):
        nonlocal check
        check += 1; emit(f'mov r15, {check}\ncmp word ptr [{address:#x}], {value}\njne failed')
    def negotiate():
        for offset, value in ((0x70, 0), (0x70, 1), (0x70, 3), (0x24, 0), (0x20, 0x10028),
                              (0x24, 1), (0x20, 1), (0x70, 11)):
            write(offset, value)
        read(0x70); checkreg(11)
    def configure(q, index):
        for offset, value in ((0x30, index), (0x38, 256), (0x80, q), (0x84, 0),
                              (0x90, q + 0x1000), (0x94, 0), (0xa0, q + 0x2000), (0xa4, 0), (0x44, 1)):
            write(offset, value)
        read(0x44); checkreg(1)
    def setup(rx, tx):
        configure(rx, 0); configure(tx, 1); write(0x70, 15); read(0x70); checkreg(15)
    def disable(index):
        write(0x30, index); write(0x44, 0); read(0x44); checkreg(0)
    def preserve(q, index, used):
        write(0x30, index); read(0x44); checkreg(1)
        checkmem(q + 0x2002, used)
    def request(q, head, lengths, packet=None, hints=False, bad=None):
        slot = 0 if head == 0 else 1
        stream = (bytes([0xf8, 0]) + b'\xff' * 8 + b'\x00\x00' if hints else bytes(12)) + (packet or b'')
        segments, cursor = [], 0
        for i, length in enumerate(lengths):
            address = q + 0x4000 + slot * 0x2000 + i * 0x800 + 0xf
            content = b'\xa5' * length if packet is None else stream[cursor:cursor + length]
            assert len(content) == length
            store(address, content); segments.append((address, length)); cursor += length
            flags = (2 if packet is None else 0) | (1 if i + 1 < len(lengths) else 0)
            if bad == 'direction' and i + 1 == len(lengths): flags |= 2
            put(q + (head + i) * 16, '<QIHH', address, length, flags, head + i + 1 if flags & 1 else 0)
        if bad == 'suffix':
            # Valid readable prefix followed by a nonempty span crossing the RAM end.
            address, length = segments[-1]
            put(q + (head + len(lengths) - 1) * 16, '<QIHH', address, length, 1, head + len(lengths))
            put(q + (head + len(lengths)) * 16, '<QIHH', 0x4000000 - 8, 16, 0, 0)
        return {'q': q, 'head': head, 'segments': segments, 'packet': packet}
    def avail(q, heads):
        put(q + 0x1002, '<H', len(heads))
        for i, head in enumerate(heads): put(q + 0x1004 + i * 2, '<H', head)
    def append(q, count, head):
        for address, value in ((q + 0x1004 + 2 * (count - 1), head), (q + 0x1002, count)):
            emit(f'mov word ptr [{address:#x}], {value}'); modifications.append((address, '<H', value))
    def wait(q, count):
        label = f'wait_{len(assembly)}'
        emit(f'{label}:\ncmp word ptr [{q + 0x2002:#x}], {count}\njne {label}')
    def complete(rxreq, txreq, rxindex, txindex):
        write(0x50, 1); wait(txreq['q'], txindex + 1); wait(rxreq['q'], rxindex + 1)
        packet = txreq['packet']; packets.append(packet.hex())
        completions.extend(((txreq, txindex, b''), (rxreq, rxindex, bytes(10) + b'\x01\x00' + packet)))
        read(0x70); checkreg(15)
    def make_pair(rx, tx, number, size=64, rxhead=0, txhead=0, split=False, hints=False, capacity=None):
        packet = frame(number, size)
        rxlength = capacity or size + 12
        r = request(rx, rxhead, [5, rxlength - 5] if split else [rxlength])
        t = request(tx, txhead, [2, 10, size] if split else [size + 12], packet, hints)
        return r, t
    def notify_rx(): write(0x50, 0)
    def invalid(rx, tx):
        notify_rx(); write(0x50, 1)
        label = f'invalid_{len(assembly)}'
        emit(f'{label}:'); read(0x70); emit(f'test eax, 64\njz {label}'); checkreg(79)
        read(0x60); checkreg(2)
        checkmem(rx + 0x2002, 0); checkmem(tx + 0x2002, 0)
        write(0x70, 0); read(0x60); checkreg(0)

    emit(f'mov r12, {MMIO:#x}')
    for offset, value in ((0, 0x74726976), (4, 2), (8, 1), (0x100, 2), (0x104, 0x00010100), (0x108, 0x05dc0000)):
        read(offset); checkreg(value)
    negotiate()
    rx, tx = BASE, BASE + 0x10000
    setup(rx, tx)
    if case == 'headers':
        first = make_pair(rx, tx, 0, split=True)
        second = make_pair(rx, tx, 1, 1518, rxhead=4, txhead=4, hints=True, capacity=1600)
        avail(rx, [0, 4]); avail(tx, [0]); notify_rx(); complete(*first, 0, 0)
        append(tx, 2, 4); complete(*second, 1, 1)
    elif case == 'short-rx':
        small_rx = request(rx, 0, [100])
        large_tx = request(tx, 0, [524], frame(2, 512))
        small_tx = request(tx, 4, [76], frame(3, 64), hints=True)
        avail(rx, [0]); avail(tx, [0]); notify_rx(); write(0x50, 1); wait(tx, 1)
        checkmem(rx + 0x2002, 0)
        packets.append(large_tx['packet'].hex()); completions.append((large_tx, 0, b''))
        # The fair owner consumes pending RX before selecting this second TX. The
        # endpoint's inline RX attempt precedes the first TX acknowledgement.
        append(tx, 2, 4); complete(small_rx, small_tx, 0, 1)
    elif case == 'queues':
        first = make_pair(rx, tx, 4); avail(rx, [0]); avail(tx, [0]); notify_rx(); complete(*first, 0, 0)
        disable(0); preserve(tx, 1, 1)
        rx2, tx2 = BASE + 0x20000, BASE + 0x30000
        second = make_pair(rx2, tx, 5, txhead=4, split=True)
        avail(rx2, [0]); configure(rx2, 0); notify_rx(); append(tx, 2, 4); complete(*second, 0, 1)
        disable(1); preserve(rx2, 0, 1)
        third = make_pair(rx2, tx2, 6, rxhead=4, hints=True)
        avail(tx2, [0]); configure(tx2, 1); append(rx2, 2, 4); complete(*third, 1, 0)
        write(0x70, 0); read(0x60); checkreg(0); negotiate()
        rx, tx = BASE + 0x40000, BASE + 0x50000
        setup(rx, tx)
        final = make_pair(rx, tx, 7); avail(rx, [0]); avail(tx, [0]); notify_rx(); complete(*final, 0, 0)
    elif case in ('active-other-queue', 'active-power'):
        first_rx, first_tx = make_pair(rx, tx, 12, split=True)
        avail(rx, [0]); avail(tx, [0]); write(0x50, 1)
        # The port reports only entry to the owned TX callback, after its RAM read.
        emit('mov edx, 0x600\nwait_tx_held:\nin al, dx\ntest al, al\njz wait_tx_held')
        checkmem(tx + 0x2002, 0); checkmem(rx + 0x2002, 0)
        packets.append(first_tx['packet'].hex()); completions.append((first_tx, 0, b''))
        if case == 'active-other-queue':
            disable(0); preserve(tx, 1, 0); checkmem(rx + 0x2002, 0)
            # Reaching this release proves disabling idle RX did not wait for held TX.
            emit('mov edx, 0x600\nmov al, 1\nout dx, al'); wait(tx, 1)
            rx2 = BASE + 0x20000
            second = make_pair(rx2, tx, 13, txhead=4, split=True)
            avail(rx2, [0]); configure(rx2, 0); append(tx, 2, 4); complete(*second, 0, 1)
        else:
            # Only the host's actual committed power-wait hook may release this callback.
            emit('xor r15d, r15d\nmov edx, 0xb004\nmov eax, 0x2000\nout dx, ax\npower_after:\nmov r15, 0xbad')
    elif case == 'halt-irq':
        arrival = frame(11, 64)
        # Leave both notification queues idle. Incoming packets schedule RX themselves;
        # only the endpoint's lifetime guard can keep this quiet guest alive.
        incoming = request(rx, 0, [5, 123]); avail(rx, [0])
        completions.append((incoming, 0, bytes(10) + b'\x01\x00' + arrival))
        handler, idt, idtr, marker = 0x120000, BASE + 0x5d000, BASE + 0x5e000, BASE + 0x5f000
        put(idt + 0x40 * 16, '<QQ', handler & 0xffff | 8 << 16 | 0x8e << 40 | (handler >> 16 & 0xffff) << 48, handler >> 32)
        put(idtr, '<HQ', 4095, idt)
        irq = f'''push rax
    mov eax, dword ptr [r12 + 0x60]
    mov dword ptr [{marker + 8:#x}], eax
    mov dword ptr [r12 + 0x64], eax
    mov eax, dword ptr [r12 + 0x60]
    mov dword ptr [{marker + 12:#x}], eax
    inc qword ptr [{marker:#x}]
    mov eax, 0xfee000b0
    mov dword ptr [rax], 0
    pop rax
    iretq
'''
        extra = [f'body {handler:#x} {ref.assemble(irq).hex()}',
                 'mmio 0xfec00000 0xfec00000 unassigned', 'mmio 0xfee00000 0xfee00000 unassigned']
        emit(f'''lidt [{idtr:#x}]
    mov edx, 0xfee00000
    mov dword ptr [rdx + 0xf0], 0x1ff
    mov dword ptr [rdx + 0x80], 0
    mov edx, 0xfec00000
    mov dword ptr [rdx], 0x33
    mov dword ptr [rdx + 0x10], 0
    mov dword ptr [rdx], 0x32
    mov dword ptr [rdx + 0x10], 0x8040
    xor r15d, r15d
    sti
    hlt
halt_after:
    cli
''')
        checkmem(marker, 1); checkmem(rx + 0x2002, 1); checkmem(tx + 0x2002, 0)
        emit(f'mov eax, dword ptr [{marker + 8:#x}]'); checkreg(1)
        emit(f'mov eax, dword ptr [{marker + 12:#x}]'); checkreg(0)
        read(0x60); checkreg(0); read(0x70); checkreg(15)
        modifications.extend(((marker, '<Q', 1), (marker + 8, '<I', 1)))
        emit('xor r15d, r15d\nmov edx, 0xb004\nmov eax, 0x2000\nout dx, ax\npower_after:\nmov r15, 0xbad')
    else:
        for number, bad in enumerate(('direction', 'suffix')):
            if number:
                rx, tx = BASE + 0x20000, BASE + 0x30000
                negotiate(); setup(rx, tx)
            request(rx, 0, [128]); request(tx, 0, [2, 74] if bad == 'direction' else [76], frame(8 + number, 64), bad=bad)
            avail(rx, [0]); avail(tx, [0]); invalid(rx, tx)
        rx, tx = BASE + 0x40000, BASE + 0x50000
        negotiate(); setup(rx, tx)
        recovery = make_pair(rx, tx, 10, split=True)
        avail(rx, [0]); avail(tx, [0]); notify_rx(); complete(*recovery, 0, 0)
    # Retire both queues before checking level IRQ ACK, avoiding a final publication race.
    if case not in ('halt-irq', 'active-power'):
        disable(0); disable(1); read(0x60); checkreg(1); write(0x64, 1); read(0x60); checkreg(0)
        emit('xor r15d, r15d')
    emit('jmp finished\nfailed:\nnop\nfinished:\nnop')
    asm = '\n'.join(assembly); code, labels = ref.assemble(asm, labels=True)
    expected = bytearray(memory)
    for address, fmt, value in modifications: struct.pack_into(fmt, expected, address - BASE, value)
    for req, index, data in completions:
        cursor = 0
        for address, length in req['segments']:
            part = data[cursor:cursor + length]
            expected[address - BASE:address - BASE + len(part)] = part; cursor += len(part)
        assert cursor == len(data)
        struct.pack_into('<II', expected, req['q'] + 0x2004 + index * 8 - BASE, req['head'], len(data))
        struct.pack_into('<H', expected, req['q'] + 0x2002 - BASE, index + 1)
    regs = [0] * 16; regs[4] = STACK + 0xff0
    lines = ['identity', f'mmio {MMIO:#x} {MMIO:#x} unassigned', f'body {CODE:#x} {code.hex()}',
             f'memory {BASE:#x} {memory.hex()}', f'region {STACK:#x} 0x1000', *extra, 'cpu', f'rip {CODE:#x}',
             'gpr ' + ' '.join(hex(v) for v in regs), 'cpl 0', 'rflags 0x2', 'if 0']
    work.mkdir(parents=True, exist_ok=True)
    (work / 'program.asm').write_text('.intel_syntax noprefix\n' + asm + '\n')
    (work / 'program.txt').write_text('\n'.join(lines) + '\n')
    (work / 'expected-memory.bin').write_bytes(expected)
    if arrival is not None:
        (work / 'arrival.bin').write_bytes(arrival)
        (work / 'idle-memory.bin').write_bytes(memory)
    meta = {'case': case, 'expected_rip': CODE + (labels['power_after'] if case in ('halt-irq', 'active-power') else len(code)), 'packets': packets,
            'expected_memory_sha256': hashlib.sha256(expected).hexdigest(), 'rx_notifications': 3 if case in ('queues', 'malformed') else 1}
    if case in ('active-other-queue', 'active-power'): meta['rx_notifications'] = 0
    if arrival is not None:
        meta.update(idle_rip=CODE + labels['halt_after'], arrival_sha256=hashlib.sha256(arrival).hexdigest(),
                    idle_memory_sha256=hashlib.sha256(memory).hexdigest(), rx_notifications=0)
    (work / 'oracle.json').write_text(json.dumps(meta, indent=2) + '\n')
    return meta


def records(stderr):
    result = []
    for prefix in ('io-state: ', 'io-memory: ', 'io-net: '):
        entries = [json.loads(line[len(prefix):]) for line in stderr.splitlines() if line.startswith(prefix)]
        if len(entries) != 1: raise ValueError('missing or duplicate final state/memory/network record')
        result.append(entries[0])
    return result


def packet_events(meta):
    events = []
    for i, packet in enumerate(meta['packets']):
        events.append('tx:' + packet)
        if i == 0 and meta['case'] in ('active-other-queue', 'active-power'):
            events.extend(['tx-held', 'release:' + ('power' if meta['case'] == 'active-power' else 'guest'), 'tx-released', 'rx:false'])
        else: events.append('rx:true')
    return events + ['closed']


def judge(meta, report, state, memory, net, expected, stderr, returncode, timeout, idle_end=False):
    errors = []
    def require(ok, reason):
        if not ok: errors.append(reason)
    require(not timeout and returncode == 0, 'VM failed or timed out')
    require('teardown: vcpus 1 joined 1 destroyed 1 vm_destroy 0 children none' in stderr.splitlines(), 'unclean teardown')
    require(len(state) == 1 and len(report['cpus']) == 1 and len(memory) == 1, 'wrong participant count')
    cpu = report['cpus'][0]
    halt_case = meta['case'] == 'halt-irq'
    require(not idle_end or halt_case, 'idle-end control applies only to halt-irq')
    stop = ('resident' if idle_end else 'poweroff') if halt_case else ('poweroff' if meta['case'] == 'active-power' else 'reached')
    require(cpu['stop'] == stop, 'wrong terminal stop')
    if idle_end: require(cpu['reason'] == 'halted status=1 detail=32', 'not the resident HALTED completion')
    rip = meta['idle_rip'] if idle_end else meta['expected_rip']
    for item in (cpu, state[0]):
        require(ref.num(item['rip']) == rip and ref.num(item['gpr'][15]) == 0, 'guest assertion failed or RIP mismatch')
    for image in (memory[0], cpu['memory']):
        require(len(image) == 1 and ref.num(image[0]['addr']) == BASE and bytes.fromhex(image[0]['data']) == expected, 'RAM differs from entire expected image')
    if halt_case:
        require(cpu['stats']['halts'] >= 1 and cpu['stats']['interrupts'] == (0 if idle_end else 1), 'wrong HLT/IRQ counts')
        if idle_end:
            require(net['events'] == ['closed'], 'packet or observation occurred in idle-end control')
        else:
            events = net['events']
            require(len(events) == 3 and events[1:] == ['arrival:true', 'closed'], 'wrong arrival/shutdown ordering or count')
            try:
                require(events[0].startswith('all-halted:'), 'missing idle observation')
                observation = json.loads(events[0].removeprefix('all-halted:'))
                first, last = observation['first'], observation['last']
                require(observation['held_us'] >= 100000 and first == last and len(first) == 1, 'unstable or short idle observation')
                word, posts = first[0]
                require(word & 255 == 1 and word >> 8 == posts & ((1 << 56) - 1), 'not resident idle state')
                require(observation['live'] == 1 and observation['kickers'] == 0 and observation['io_active'] is False, 'extra source veto')
            except (ValueError, KeyError, TypeError, IndexError): require(False, 'invalid idle observation')
    else:
        events = packet_events(meta)
        require(net['events'] == events, 'wrong packet bytes/count, receive admission, or endpoint shutdown')
    require(report['io_net'] == net, 'network stdout/stderr mismatch')
    return errors


def run_case(args, case):
    work = args.out / case; meta = fixture(case, work)
    command = [str(args.host), str(args.runtime), 'run-program', str(args.fexc), str(work / 'program.txt'), str(work),
               '--watchdog-ms', '30000', '--ram', '0x4000000', '--code-shape', '--io-net']
    if case == 'halt-irq': command += ['--io-net-arrival', str(work / 'arrival.bin')]
    if case in ('active-other-queue', 'active-power'):
        command += ['--io-net-hold', 'power' if case == 'active-power' else 'guest']
    (work / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    started, timeout = time.monotonic(), False
    with (work / 'stdout.log').open('wb') as stdout, (work / 'stderr.log').open('wb') as stderr:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
        try: process.wait(timeout=120)
        except subprocess.TimeoutExpired: timeout = True; stop_group(process)
        except BaseException: stop_group(process); raise
    stderr = (work / 'stderr.log').read_text(errors='replace')
    result = {'case': case, 'expect_idle_end': args.expect_idle_end, 'returncode': process.returncode, 'timeout': timeout, 'seconds': time.monotonic() - started}
    try:
        state, memory, net = records(stderr)
        report = json.loads((work / 'stdout.log').read_text())
        result['io_net'] = net
        expected = work / ('idle-memory.bin' if args.expect_idle_end else 'expected-memory.bin')
        result['errors'] = judge(meta, report, state, memory, net, expected.read_bytes(), stderr, process.returncode, timeout, args.expect_idle_end)
    except (ValueError, KeyError, TypeError, IndexError) as error: result['errors'] = [f'invalid report: {error}']
    result['pass'] = not result['errors']
    (work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(f"{case}: {'PASS' if result['pass'] else 'FAIL'} ({result['seconds']:.2f}s) {result['errors']}", flush=True)
    return result


def self_check():
    controls = 0
    lines = ['io-state: []', 'io-memory: []', 'io-net: {}']
    assert records('\n'.join(lines)) == [[], [], {}]
    for i in range(3):
        for bad in (lines[:i] + lines[i + 1:], lines + [lines[i]]):
            try: records('\n'.join(bad))
            except ValueError: controls += 1
            else: raise AssertionError('accepted missing/duplicate record')
    with tempfile.TemporaryDirectory() as tmp:
        for case in CASES:
            work = pathlib.Path(tmp) / case; meta = fixture(case, work)
            expected = (work / 'expected-memory.bin').read_bytes()
            state = [{'rip': hex(meta['expected_rip']), 'gpr': [0] * 16}]
            image = [{'addr': hex(BASE), 'data': expected.hex()}]
            net = {'events': packet_events(meta)}
            report = {'cpus': [{**copy.deepcopy(state[0]), 'stop': 'poweroff' if case == 'active-power' else 'reached', 'memory': copy.deepcopy(image)}], 'io_net': copy.deepcopy(net)}
            if case == 'halt-irq':
                observation = {'held_us': 100000, 'first': [[1, 0]], 'last': [[1, 0]], 'live': 1, 'kickers': 0, 'io_active': False}
                net = {'events': ['all-halted:' + json.dumps(observation), 'arrival:true', 'closed']}
                report['io_net'] = copy.deepcopy(net)
                report['cpus'][0].update(stop='poweroff', stats={'halts': 1, 'interrupts': 1})
            args = [meta, report, state, [image], net, expected, 'teardown: vcpus 1 joined 1 destroyed 1 vm_destroy 0 children none\n', 0, False]
            assert not judge(*args)
            for mutation in ('state', 'rip', 'stop', 'post-memory', 'report-memory', 'packet', 'extra-packet', 'closed', 'receive-drop', 'teardown', 'exit', 'timeout'):
                bad = copy.deepcopy(args)
                if mutation == 'state': bad[2][0]['gpr'][15] = 1
                elif mutation == 'rip': bad[1]['cpus'][0]['rip'] = '0'
                elif mutation == 'stop': bad[1]['cpus'][0]['stop'] = 'halted'
                elif mutation == 'post-memory': bad[3][0][0]['data'] = 'ff' + expected.hex()[2:]
                elif mutation == 'report-memory': bad[1]['cpus'][0]['memory'][0]['data'] = 'ff' + expected.hex()[2:]
                elif mutation == 'packet': bad[4]['events'][0] = 'tx:bad'
                elif mutation == 'extra-packet': bad[4]['events'].insert(0, 'tx:bad')
                elif mutation == 'closed': bad[4]['events'].pop()
                elif mutation == 'receive-drop': bad[4]['events'][1] = 'rx:false'
                elif mutation == 'teardown': bad[6] = ''
                elif mutation == 'exit': bad[7] = 1
                else: bad[8] = True
                assert judge(*bad), (case, mutation); controls += 1
            if case in ('active-other-queue', 'active-power'):
                for index in (1, 2, 3, 4):
                    bad = copy.deepcopy(args); bad[4]['events'].pop(index)
                    bad[1]['io_net'] = copy.deepcopy(bad[4])
                    assert judge(*bad), (case, index); controls += 1
                bad = copy.deepcopy(args); bad[4]['events'][4] = 'rx:true'
                bad[1]['io_net'] = copy.deepcopy(bad[4])
                assert judge(*bad); controls += 1
            if case == 'halt-irq':
                for key, value in [('held_us', 99999), ('last', [[257, 1]]), ('live', 2), ('kickers', 1), ('io_active', True), ('first', [[0, 0]])]:
                    bad = copy.deepcopy(args); observation_bad = {**observation, key: value}
                    bad[4]['events'][0] = 'all-halted:' + json.dumps(observation_bad)
                    bad[1]['io_net'] = copy.deepcopy(bad[4])
                    assert judge(*bad), key; controls += 1
                bad = copy.deepcopy(args); bad[1]['cpus'][0]['stats']['interrupts'] = 0
                assert judge(*bad); controls += 1
                idle = copy.deepcopy(args)
                idle[1]['cpus'][0].update(stop='resident', reason='halted status=1 detail=32', rip=hex(meta['idle_rip']), stats={'halts': 1, 'interrupts': 0})
                idle[2][0]['rip'] = hex(meta['idle_rip'])
                idle[4] = {'events': ['closed']}; idle[1]['io_net'] = copy.deepcopy(idle[4])
                idle[5] = (work / 'idle-memory.bin').read_bytes()
                idle[3][0][0]['data'] = idle[5].hex(); idle[1]['cpus'][0]['memory'][0]['data'] = idle[5].hex()
                idle.append(True)
                assert not judge(*idle)
                for key, value in [('stop', 'cancelled'), ('reason', 'bad-link status=1 detail=32'), ('rip', '0'), ('stats', {'halts': 1, 'interrupts': 1})]:
                    bad = copy.deepcopy(idle); bad[1]['cpus'][0][key] = value
                    assert judge(*bad); controls += 1
    print(f'PASS: assembled {len(CASES)} guests; {controls} negative controls')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True); sub.add_parser('self-check')
    prepare = sub.add_parser('prepare'); prepare.add_argument('--out', type=pathlib.Path, required=True)
    run = sub.add_parser('run')
    for key in ('host', 'runtime', 'fexc', 'out'): run.add_argument('--' + key, type=pathlib.Path, required=True)
    run.add_argument('--cases', nargs='+', choices=CASES, default=list(CASES))
    run.add_argument('--expect-idle-end', action='store_true', help='isolated missing-guard control only: require Halted without RX/IRQ effects')
    run.add_argument('--lock', type=pathlib.Path, default=VM_LOCK)
    args = parser.parse_args()
    if args.action == 'self-check': self_check(); return 0
    args.out = args.out.resolve(); args.out.mkdir(parents=True, exist_ok=False)
    if args.action == 'prepare':
        for case in CASES: fixture(case, args.out / case)
        return 0
    if len(set(args.cases)) != len(args.cases): parser.error('no duplicate cases')
    if args.expect_idle_end and args.cases != ['halt-irq']: parser.error('idle-end control requires exactly halt-irq')
    for key in ('host', 'runtime', 'fexc'): setattr(args, key, getattr(args, key).resolve(strict=True))
    sources = [p for p in (ROOT / 'crates').rglob('*') if p.is_file()]
    sources += [ROOT / p for p in ('Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'tools/run_virtio_net.py', 'tools/run_io_windows.py', 'tools/reference/ref.py', 'tools/reference/flags.py')]
    manifest = {'purpose': 'network frontend development; no native-network acceptance or performance claims',
                'source_root': str(ROOT), 'source_sha256': {str(p.relative_to(ROOT)): digest(p) for p in sorted(sources)},
                'artifacts': {key: {'path': str(getattr(args, key)), 'sha256': digest(getattr(args, key))} for key in ('host', 'runtime', 'fexc')},
                'limits': {'vm_launches': len(args.cases), 'seconds_per_vm': 120, 'cpus': 1}, 'expect_idle_end': args.expect_idle_end,
                'excluded': ['native networking and privileges', 'SMP reset peers', 'performance', 'concurrent malicious ring mutation']}
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    results = []
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for case in args.cases:
            result = run_case(args, case); results.append(result)
            (args.out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
            if not result['pass']: break
    return 0 if len(results) == len(args.cases) and all(r['pass'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
