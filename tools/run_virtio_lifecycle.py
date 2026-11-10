#!/usr/bin/env python3
"""Deterministic block lifecycle checks through ordinary guest virtio MMIO.

Uses the testing driver's native-call schedule only; queue validation, copies,
reset, IRQ and power drain are production paths. Run from a retained source build.
Thirteen N=1 cases and one N=2 power/HLT case, no retries, at most 120 seconds per VM. Three
cases force concurrent-request schedules: out-of-order completion, a canceled completion window
covering four requests, and a reset with requests completed, running and queued. No throughput
claims.
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

BASE, CODE, STACK, MMIO = 0x400000, 0x100000, 0x300000, 0xf0000000
SIZE, DISK_SIZE = 0x20000, 2 * 1024 * 1024
CASES = ('reset', 'disable', 'power-scalar', 'power-rep', 'power-sync-error',
         'read-error', 'write-error', 'malformed', 'malformed-later', 'fullqueue', 'power-rep-halt',
         'out-of-order', 'cancel-batch', 'reset-mixed')
MODES = {'reset': 'hold-reset', 'disable': 'hold-disable', 'power-scalar': 'hold-power',
         'power-rep': 'hold-power', 'power-sync-error': 'power-sync-error', 'power-rep-halt': 'hold-power-halt',
         'out-of-order': 'out-of-order', 'cancel-batch': 'cancel-batch', 'reset-mixed': 'reset-mixed'}
# The device reset's register readback (offset, saved at, value), shared by the reset cases.
RESET_READBACK = ((0x60, 0xe000, 0), (0x70, 0xe004, 0), (0x44, 0xe008, 0))
PEER_CODE, PEER_MARKER, PEER_R15 = 0x110000, 0x48414c5450454552, 0x5151


def pattern(label, size):
    return b''.join(hashlib.sha256(label + i.to_bytes(8, 'little')).digest()
                    for i in range((size + 31) // 32))[:size]


def put(memory, address, fmt, *values):
    struct.pack_into(fmt, memory, address - BASE, *values)


def store(memory, address, data):
    memory[address - BASE:address - BASE + len(data)] = data


def fixture(case, work):
    """Generate one guest and its complete RAM/file oracle before execution."""
    memory = bytearray(SIZE)
    disk = pattern(b'natemu-lifecycle-disk-v1', DISK_SIZE)
    payload = pattern(b'natemu-lifecycle-out-v1', 512)
    assembly, check = [], 0

    def emit(text): assembly.append(text)
    def expect(operand, value, width='dword'):
        nonlocal check
        check += 1
        emit(f'mov r15, {check}\ncmp {width} ptr [{operand:#x}], {value}\njne failed')
    def reg(value):
        nonlocal check
        check += 1
        emit(f'mov r15, {check}\ncmp eax, {value}\njne failed')
    def mmwrite(offset, value): emit(f'mov dword ptr [r12 + {offset:#x}], {value}')
    def readreg(offset): emit(f'mov eax, dword ptr [r12 + {offset:#x}]')
    def negotiate():
        for offset, value in ((0x70, 0), (0x70, 1), (0x70, 3), (0x24, 0),
                              (0x20, 0x206), (0x24, 1), (0x20, 1), (0x70, 11)):
            mmwrite(offset, value)
        readreg(0x70); reg(11)
    def configure(q):
        for offset, value in ((0x30, 0), (0x38, 128), (0x80, q), (0x84, 0),
                              (0x90, q + 0x1000), (0x94, 0), (0xa0, q + 0x2000),
                              (0xa4, 0), (0x44, 1), (0x70, 15)):
            mmwrite(offset, value)
        readreg(0x44); reg(1)
        readreg(0x70); reg(15)
    def desc(q, i, addr, length, flags=0, next_=0):
        put(memory, q + i * 16, '<QIHH', addr, length, flags, next_)
    def request(q, head, kind, sector=0, malformed=False):
        header, status, data = q + 0x3000 + head * 32, q + 0x5000 + head, q + 0x6000 + head * 512
        put(memory, header, '<IIQ', kind, 0, sector)
        store(memory, status, b'\xa5')
        if kind == 4:
            desc(q, head, header, 16, 1, head + 1)
            desc(q, head + 1, status, 1, 2)
        else:
            store(memory, data, payload if kind == 1 else b'\xa5' * 512)
            desc(q, head, header, 16, 1, head + 1)
            if malformed:
                desc(q, head + 1, data, 256, 3, head + 2)
                desc(q, head + 2, 0x4000000 - 128, 256, 3, head + 3)
                desc(q, head + 3, status, 1, 2)
            else:
                desc(q, head + 1, data, 512, 3 if kind == 0 else 1, head + 2)
                desc(q, head + 2, status, 1, 2)
        return {'head': head, 'status': status, 'data': data, 'kind': kind, 'sector': sector}
    def avail(q, heads):
        put(memory, q + 0x1002, '<H', len(heads))
        for i, head in enumerate(heads): put(memory, q + 0x1004 + 2 * i, '<H', head)
    def wait_used(q, n):
        label = f'used_{q:x}_{n}'
        emit(f'{label}:\ncmp word ptr [{q + 0x2002:#x}], {n}\njne {label}')
    def irq(q, bit=1):
        # Wait for every bit: a concurrent completion may raise its own after another's.
        label = f'irq_{len(assembly)}'
        emit(f'{label}:'); readreg(0x60)
        emit(f'mov ecx, eax\nand ecx, {bit}\ncmp ecx, {bit}\njne {label}\nmov dword ptr [{q + 0xe00c:#x}], eax')
        reg(bit); mmwrite(0x64, bit); readreg(0x60); reg(0)
        emit(f'mov dword ptr [{q + 0xe010:#x}], eax')
    def hold():
        emit('mov edx, 0x600\nheld:\nin al, dx\ntest al, al\njz held')
    def notify(): mmwrite(0x50, 0)
    def submit_next(q, head, count):
        emit(f'mov word ptr [{q + 0x1004 + 2 * (count - 1):#x}], {head}\n'
             f'mov word ptr [{q + 0x1002:#x}], {count}')
        notify(); wait_used(q, count); irq(q)

    emit(f'mov r12, {MMIO:#x}')
    readreg(0); reg(0x74726976)
    readreg(4); reg(2)
    readreg(8); reg(2)
    negotiate(); configure(BASE)
    q, completed, later_avail, quiet = BASE, [], [], []
    expected_disk = bytearray(disk)
    canceled = case in ('reset', 'disable', 'malformed')
    power = case.startswith('power-')
    if case == 'malformed-later':
        first = request(q, 0, 0)
        second = request(q, 3, 0, 1, malformed=True)
        # Both heads are published together: a valid first completion must not admit the
        # malformed captured successor or touch any of its payload/status bytes.
        avail(q, [0, 3]); notify(); wait_used(q, 1)
        emit('malformed_later_wait:'); readreg(0x70)
        emit('test eax, 64\njz malformed_later_wait')
        emit(f'mov dword ptr [{q + 0xe014:#x}], eax')
        irq(q, 3)
        expect(q + 0x2002, 1, 'word'); expect(first['status'], 0, 'byte'); expect(second['status'], 0xa5, 'byte')
        mmwrite(0x70, 0)
        completed.append((q, 0, first, 0, disk[:512]))
    elif case == 'out-of-order':
        # The sector-0 read is held until the sector-1 read has been published, so used slots
        # follow completion order, not admission order.
        first, second = request(q, 0, 0, 0), request(q, 3, 0, 1)
        avail(q, [0, 3]); notify(); wait_used(q, 2)
        mmwrite(0x44, 0); readreg(0x44); reg(0); irq(q)
        completed += [(q, 0, second, 0, disk[512:1024]), (q, 1, first, 0, disk[:512])]
    elif case in ('cancel-batch', 'reset-mixed'):
        if case == 'cancel-batch':
            # Four reads finish; the completion covering them is held until the reset defers
            # behind them, so that window is never granted.
            old, published = [request(q, 3 * i, 0, i) for i in range(4)], 0
        else:
            # A read and an unsupported request complete (the immediate answer first); five
            # sector-0 reads are then held on the four workers or queued when the reset defers.
            old = [request(q, 0, 0, 1), request(q, 3, 123)] + [request(q, 6 + 3 * i, 0, 0) for i in range(5)]
            published = 2
            quiet += [(q, 0, old[1], 2, b''), (q, 1, old[0], 0, disk[512:1024])]
        avail(q, [r['head'] for r in old]); notify()
        if published: wait_used(q, published)
        hold(); mmwrite(0x70, 0)
        for offset, saved, value in RESET_READBACK:
            readreg(offset); emit(f'mov dword ptr [{q + saved:#x}], eax'); reg(value)
        expect(q + 0x2002, published, 'word')
        for r in old[published:]: expect(r['status'], 0xa5, 'byte')
        fresh = request(q + 0x10000, 0, 0, 4)
        avail(q + 0x10000, [0]); negotiate()
        configure(q + 0x10000); notify(); wait_used(q + 0x10000, 1); irq(q + 0x10000)
        completed.append((q + 0x10000, 0, fresh, 0, disk[2048:2560]))
    elif case == 'fullqueue':
        requests = [request(q, i, 4) for i in range(0, 128, 2)]
        avail(q, [r['head'] for r in requests]); notify(); wait_used(q, 64)
        # Close after RAM publication so a final IRQ cannot race the acknowledgement.
        mmwrite(0x44, 0); readreg(0x44); reg(0); irq(q)
        completed += [(q, i, r, 0, b'') for i, r in enumerate(requests)]
    else:
        kind = 1 if power or case == 'write-error' else 0
        first = request(q, 0, kind, 8 if kind == 1 else 0, case == 'malformed')
        avail(q, [0]); notify()
        if power:
            hold()
            put(memory, BASE + 0xf000, '<HH', 0x2000, 0x2000)
            emit('xor r15d, r15d\nmov edx, 0xb004')
            if case in ('power-rep', 'power-rep-halt'):
                emit(f'mov esi, {BASE + 0xf000:#x}\nmov ecx, 2\npower_instruction:\nrep outsw\npower_after:')
            else:
                emit('mov eax, 0x2000\npower_instruction:\nout dx, ax\npower_after:')
            emit('mov r15, 0xbad')
            expected_disk[4096:4608] = payload
            completed.append((q, 0, first, 0, b''))
        elif canceled:
            if case == 'malformed':
                emit('malformed_wait:'); readreg(0x70)
                emit('test eax, 64\njz malformed_wait')
                emit(f'mov dword ptr [{q + 0xe014:#x}], eax')
                irq(q, 2)
            else: hold()
            mmwrite(0x44 if case == 'disable' else 0x70, 0)
            for offset, saved, value in ((0x60, 0xe000, 0), (0x70, 0xe004, 15 if case == 'disable' else 0), (0x44, 0xe008, 0)):
                readreg(offset); emit(f'mov dword ptr [{q + saved:#x}], eax'); reg(value)
            expect(q + 0x2002, 0, 'word'); expect(first['status'], 0xa5, 'byte')
            fresh = request(q + 0x10000, 0, 0, 1)
            avail(q + 0x10000, [0])
            if case != 'disable': negotiate()
            configure(q + 0x10000); notify(); wait_used(q + 0x10000, 1); irq(q + 0x10000)
            completed.append((q + 0x10000, 0, fresh, 0, disk[512:1024]))
        else:
            wait_used(q, 1); irq(q)
            expect(first['status'], 1, 'byte')
            completed.append((q, 0, first, 1, b''))
            second = request(q, 3, 0, 8 if case == 'write-error' else 1)
            if case == 'write-error': expected_disk[4096:4352] = payload[:256]
            submit_next(q, 3, 2); later_avail.append((q, 2, 3))
            data = bytes(expected_disk[4096:4608]) if case == 'write-error' else disk[512:1024]
            completed.append((q, 1, second, 0, data))
            if case == 'write-error':
                flush = request(q, 6, 4)
                submit_next(q, 6, 3); later_avail.append((q, 3, 6))
                completed.append((q, 2, flush, 0, b''))
            readreg(0x70); reg(15)
    if not power: emit('xor r15d, r15d')
    emit('jmp finished\nfailed:\nnop\nfinished:\nnop')
    asm = '\n'.join(assembly)
    code, labels = ref.assemble(asm, labels=True)
    expected = bytearray(memory)
    for target, index, req, status, data in completed + quiet:
        store(expected, req['status'], bytes([status]))
        if data: store(expected, req['data'], data)
        length = 0 if req['kind'] == 0 and status else len(data) + 1
        put(expected, target + 0x2004 + 8 * index, '<II', req['head'], length)
        put(expected, target + 0x2002, '<H', index + 1)
    # Completions the guest acknowledged by interrupt (not those a reset overtook).
    for target, *_ in completed:
        if not power: put(expected, target + 0xe00c, '<I', 1)
    for target, count, head in later_avail:
        put(expected, target + 0x1002, '<H', count)
        put(expected, target + 0x1004 + 2 * (count - 1), '<H', head)
    if case == 'disable': put(expected, BASE + 0xe004, '<I', 15)
    if case in ('malformed', 'malformed-later'):
        put(expected, BASE + 0xe014, '<I', 15 | 64)
        put(expected, BASE + 0xe00c, '<I', 3 if case == 'malformed-later' else 2)
    regs = [0] * 16; regs[4] = STACK + 0xff0
    lines = ['identity', f'mmio {MMIO:#x} {MMIO:#x} unassigned', f'body {CODE:#x} {code.hex()}',
             f'memory {BASE:#x} {memory.hex()}', f'region {STACK:#x} 0x1000']
    peer_rip = None
    work.mkdir(parents=True, exist_ok=True)
    if case == 'power-rep-halt':
        helper = f'mov r14, {PEER_MARKER:#x}\ncli\nhlt\nhelper_after:\nmov r15, 0xbad\nnop\n'
        peer_code, peer_labels = ref.assemble(helper, labels=True)
        peer_rip = PEER_CODE + peer_labels['helper_after']
        lines += [f'body {PEER_CODE:#x} {peer_code.hex()}', f'region {STACK + 0x1000:#x} 0x1000']
        (work / 'peer.asm').write_text('.intel_syntax noprefix\n' + helper)
    lines += ['cpu', f'rip {CODE:#x}', 'gpr ' + ' '.join(hex(v) for v in regs), 'cpl 0', 'rflags 0x2', 'if 0']
    if case == 'power-rep-halt':
        peer_regs = [0] * 16; peer_regs[4], peer_regs[15] = STACK + 0x1ff0, PEER_R15
        lines += ['cpu', f'rip {PEER_CODE:#x}', 'gpr ' + ' '.join(hex(v) for v in peer_regs), 'cpl 0', 'rflags 0x2', 'if 0']
    (work / 'program.asm').write_text('.intel_syntax noprefix\n' + asm + '\n')
    (work / 'program.txt').write_text('\n'.join(lines) + '\n')
    (work / 'disk.raw').write_bytes(disk)
    (work / 'expected-disk.raw').write_bytes(expected_disk)
    (work / 'expected-memory.bin').write_bytes(expected)
    meta = {'case': case, 'n': 2 if peer_rip is not None else 1, 'peer_rip': peer_rip, 'mode': MODES.get(case, case if case in ('read-error', 'write-error') else 'normal'),
            'power': power, 'sync_error': case == 'power-sync-error', 'expected_rip': CODE + (labels['power_instruction'] if case in ('power-rep', 'power-rep-halt') else labels['power_after']) if power else CODE + len(code),
            'disk_before_sha256': hashlib.sha256(disk).hexdigest(),
            'disk_after_sha256': hashlib.sha256(expected_disk).hexdigest(),
            'memory_after_sha256': hashlib.sha256(expected).hexdigest()}
    (work / 'oracle.json').write_text(json.dumps(meta, indent=2) + '\n')
    return meta


def ordered(events, sequence):
    cursor = 0
    for event in events:
        if cursor < len(sequence) and event == sequence[cursor]: cursor += 1
    return cursor == len(sequence)


def verify(meta, report, state, io, stderr, returncode, timed_out, disk, expected_disk, expected_memory, post_memory):
    errors, case, n = [], meta['case'], meta['n']
    def require(ok, reason):
        if not ok: errors.append(reason)
    require(not timed_out, 'VM timeout')
    require(returncode != 0 if meta['sync_error'] else returncode == 0, 'unexpected process status')
    require(f'teardown: vcpus {n} joined {n} destroyed {n} vm_destroy 0 children none' in stderr.splitlines(), 'unclean teardown')
    require(disk == expected_disk, 'disk differs from exact expected contents')
    require(len(post_memory) == n and all(len(memory) == 1
            and ref.num(memory[0]['addr']) == BASE
            and bytes.fromhex(memory[0]['data']) == expected_memory for memory in post_memory),
            'post-VM RAM differs from complete expected image')
    events = io['events']
    require(io['entered'] == (case != 'fullqueue'), 'unexpected positioned-I/O entry state')
    require(not any(e in events for e in ('barrier-timeout', 'trace-overflow')), 'invalid test schedule')
    require(len(state) == n, 'wrong state count')
    cpu = state[0]
    require(ref.num(cpu['rip']) == meta['expected_rip'], 'architectural RIP mismatch')
    require(ref.num(cpu['gpr'][15]) == 0, 'guest assertion or forbidden post-power instruction ran')
    if case in ('power-rep', 'power-rep-halt'):
        require(ref.num(cpu['gpr'][1]) == 1 and ref.num(cpu['gpr'][6]) == BASE + 0xf002, 'REP progress mismatch')
    if not meta['sync_error']:
        require(len(report['cpus']) == n, 'wrong CPU count')
        cpu = report['cpus'][0]
        require(cpu['stop'] == ('poweroff' if meta['power'] else 'reached'), 'wrong terminal stop')
        require(ref.num(cpu['rip']) == meta['expected_rip'], 'report/state RIP mismatch')
        for v, item in enumerate(report['cpus']):
            memories = item['memory']
            require(len(memories) == 1 and ref.num(memories[0]['addr']) == BASE and bytes.fromhex(memories[0]['data']) == expected_memory, f'CPU {v} RAM differs from complete expected image')
        if case == 'power-rep-halt':
            peer = report['cpus'][1]
            # Power drain releases the disk's busy guard after its final sync; the driver's halted rule may then end the
            # sleeping peer (nothing can wake it) before the power stop cancels it. Either ending is correct.
            ended = peer['stop'] == 'cancelled' or peer['stop'] == 'resident' and peer['reason'].startswith('halted ')
            require(ended and peer['stats']['halts'] > 0, 'peer did not halt then end')
            for record in (state[1], peer):
                require(ref.num(record['rip']) == meta['peer_rip'], 'peer advanced beyond HLT')
                require(ref.num(record['gpr'][14]) == PEER_MARKER and ref.num(record['gpr'][15]) == PEER_R15, 'peer marker/progress mismatch')
        require(report['io_disk'] == io, 'stderr/stdout trace mismatch')
    else:
        require('disk shutdown full-sync:' in stderr, 'wrong contained shutdown error')
        require(not report, 'failed full-sync reported normal power success')
    if case in ('reset', 'disable'):
        boundary = 'reset-deferred' if case == 'reset' else 'disable-deferred'
        require(ordered(events, ['backend-held', boundary, 'backend-released', 'read:0:512', 'read-result:Ok(512)', 'read:512:512', 'read-result:Ok(512)', 'commit']), 'reset/disable schedule mismatch')
        require(events.count('commit') == 1 and io['calls'] == 2, 'canceled queue published or recovery missing')
    elif meta['power']:
        end = 'sync-error' if meta['sync_error'] else 'sync-ok'
        sequence = ['backend-held', 'power-poll', 'backend-released', 'write:4096:512', 'write-result:Ok(512)', 'commit', 'sync-enter', end]
        if case == 'power-rep-halt': sequence.insert(2, 'peer-halted')
        if not meta['sync_error']: sequence += ['lane-joined']
        require(ordered(events, sequence), 'power drain order mismatch')
        require(io['calls'] == 1 and events.count('commit') == 1, 'power request duplicated or lost')
        require(('lane-joined' not in events) if meta['sync_error'] else True, 'failed sync acknowledged quiescence')
    elif case in ('read-error', 'write-error'):
        verb, offset = ('read', 0) if case == 'read-error' else ('write', 4096)
        recovery = 512 if case == 'read-error' else 4096
        require(ordered(events, [f'{verb}:{offset}:512', f'{verb}-result:Ok(256)', f'{verb}:{offset + 256}:256', 'injected-eio', 'commit', f'read:{recovery}:512', 'read-result:Ok(512)', 'commit']), 'partial-I/O recovery order mismatch')
        require(io['calls'] == 3 and events.count('injected-eio') == 1, 'wrong partial I/O attempt count')
        require(events.count('commit') == (3 if case == 'write-error' else 2), 'wrong completion count')
        if case == 'write-error': require(ordered(events, ['sync-enter', 'sync-ok', 'commit']), 'partial write not flushed')
    elif case == 'out-of-order':
        require(io['calls'] == 2 and events.count('commit') == 2 and events.count('backend-held') == 1, 'wrong call or completion count')
        require(ordered(events, ['read:512:512', 'read-result:Ok(512)', 'commit', 'backend-released', 'read:0:512', 'read-result:Ok(512)', 'commit']),
                'held read did not complete after the later one')
    elif case == 'cancel-batch':
        require(io['calls'] == 5 and events.count('commit') == 1 and events.count('read-result:Ok(512)') == 5, 'canceled batch published or recovery missing')
        held = events.index('batch-held') if 'batch-held' in events else -1
        require(all(f'read:{512 * i}:512' in events[:held] for i in range(4)) and events[:held].count('read-result:Ok(512)') == 4,
                'batch held before its four reads finished')
        require(ordered(events, ['batch-held', 'reset-deferred', 'batch-released', 'read:2048:512', 'read-result:Ok(512)', 'commit']), 'canceled batch order mismatch')
    elif case == 'reset-mixed':
        require(io['calls'] == 7 and events.count('commit') == 3 and events.count('read:0:512') == 5, 'wrong call or completion count')
        require(events.count('backend-held') == 5 and events.count('read-result:Ok(512)') == 7, 'held sector-0 reads missing')
        require(ordered(events, ['commit', 'commit', 'reset-deferred', 'backend-released', 'read:2048:512', 'read-result:Ok(512)', 'commit']),
                'mixed reset order mismatch')
    elif case == 'malformed-later':
        require(io['calls'] == 1 and events.count('commit') == 1, 'malformed captured successor reached backend or publication')
        require(ordered(events, ['read:0:512', 'read-result:Ok(512)', 'commit']), 'valid captured prefix did not complete')
    elif case == 'malformed':
        require(io['calls'] == 1 and events.count('commit') == 1, 'malformed chain reached backend or publication')
        require(ordered(events, ['read:512:512', 'read-result:Ok(512)', 'commit']), 'malformed reset recovery missing')
    else:
        require(io['calls'] == 0 and events.count('commit') == 64 and events.count('sync-ok') == 64, 'full descriptor queue did not drain exactly once')
    return errors


def final_records(stderr):
    result = []
    for prefix in ('io-state: ', 'io-disk: ', 'io-memory: '):
        records = [json.loads(line[len(prefix):]) for line in stderr.splitlines() if line.startswith(prefix)]
        if len(records) != 1:
            raise ValueError('missing or duplicate final state/trace/memory')
        result.append(records[0])
    return result


def run_case(args, case):
    work = args.out / case
    meta = fixture(case, work)
    command = [str(args.host), str(args.runtime), 'run-program', str(args.fexc), str(work / 'program.txt'), str(work),
               '--watchdog-ms', '30000', '--ram', '0x4000000', '--code-shape',
               '--io-disk', str(work / 'disk.raw'), '--io-script', meta['mode']]
    (work / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    started, timed_out = time.monotonic(), False
    with (work / 'stdout.log').open('wb') as stdout, (work / 'stderr.log').open('wb') as stderr:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
        try: process.wait(timeout=120)
        except subprocess.TimeoutExpired: timed_out = True; stop_group(process)
        except BaseException: stop_group(process); raise
    stderr = (work / 'stderr.log').read_text(errors='replace')
    result = {'case': case, 'returncode': process.returncode, 'timeout': timed_out, 'seconds': time.monotonic() - started}
    try:
        stdout = (work / 'stdout.log').read_text()
        report = json.loads(stdout) if stdout.strip() else {}
        state, trace, memory = final_records(stderr)
        result['io_disk'] = trace
        result['errors'] = verify(meta, report, state, trace, stderr, process.returncode, timed_out,
                                  (work / 'disk.raw').read_bytes(), (work / 'expected-disk.raw').read_bytes(),
                                  (work / 'expected-memory.bin').read_bytes(), memory)
    except (ValueError, KeyError, TypeError, IndexError) as error: result['errors'] = [f'invalid report: {error}']
    result['pass'] = not result['errors']
    (work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(f"{case}: {'PASS' if result['pass'] else 'FAIL'} ({result['seconds']:.2f}s) {result['errors']}", flush=True)
    return result


def self_check():
    # Assemble all real guests; the oracle controls are filled from explicit successful traces.
    traces = {
        'reset': ['backend-held', 'reset-deferred', 'backend-released', 'read:0:512', 'read-result:Ok(512)', 'read:512:512', 'read-result:Ok(512)', 'commit'],
        'disable': ['backend-held', 'disable-deferred', 'backend-released', 'read:0:512', 'read-result:Ok(512)', 'read:512:512', 'read-result:Ok(512)', 'commit'],
        'read-error': ['read:0:512', 'read-result:Ok(256)', 'read:256:256', 'injected-eio', 'commit', 'read:512:512', 'read-result:Ok(512)', 'commit'],
        'write-error': ['write:4096:512', 'write-result:Ok(256)', 'write:4352:256', 'injected-eio', 'commit', 'read:4096:512', 'read-result:Ok(512)', 'commit', 'sync-enter', 'sync-ok', 'commit'],
        'malformed': ['read:512:512', 'read-result:Ok(512)', 'commit'],
        'malformed-later': ['read:0:512', 'read-result:Ok(512)', 'commit'],
        'fullqueue': ['sync-enter', 'sync-ok', 'commit'] * 64,
        'out-of-order': ['backend-held', 'read:512:512', 'read-result:Ok(512)', 'commit', 'backend-released', 'read:0:512', 'read-result:Ok(512)', 'commit'],
        'cancel-batch': [e for i in range(4) for e in (f'read:{512 * i}:512', 'read-result:Ok(512)')]
        + ['batch-held', 'reset-deferred', 'batch-released', 'read:2048:512', 'read-result:Ok(512)', 'commit'],
        'reset-mixed': ['read:512:512', 'read-result:Ok(512)', 'commit', 'commit'] + ['backend-held'] * 4 + ['reset-deferred']
        + ['backend-released', 'read:0:512', 'read-result:Ok(512)'] * 4 + ['backend-held', 'backend-released', 'read:0:512', 'read-result:Ok(512)']
        + ['read:2048:512', 'read-result:Ok(512)', 'commit'],
    }
    # Schedule corruptions each concurrent case must reject.
    corrupt = {
        'out-of-order': lambda e: e[:3] + [e[4], e[3]] + e[5:],  # released before the first commit
        'cancel-batch': lambda e: e[:-1] + ['commit', 'commit'],
        'reset-mixed': lambda e: e[:4] + e[5:],
    }
    controls = 0
    record_lines = ['io-state: []', 'io-disk: {}', 'io-memory: []']
    assert final_records('\n'.join(record_lines)) == [[], {}, []]
    for i in range(3):
        for invalid in (record_lines[:i] + record_lines[i + 1:], record_lines + [record_lines[i]]):
            try: final_records('\n'.join(invalid))
            except ValueError: controls += 1
            else: raise AssertionError('accepted missing/duplicate final record')
    with tempfile.TemporaryDirectory() as tmp:
        for case in CASES:
            work = pathlib.Path(tmp) / case
            meta = fixture(case, work)
            events = traces.get(case, ['backend-held', 'power-poll', 'backend-released', 'write:4096:512', 'write-result:Ok(512)', 'commit', 'sync-enter'] + (['sync-error'] if meta['sync_error'] else ['sync-ok', 'lane-joined']))
            if case == 'power-rep-halt': events.insert(2, 'peer-halted')
            calls = {'fullqueue': 0, 'reset': 2, 'disable': 2, 'read-error': 3, 'write-error': 3, 'out-of-order': 2, 'cancel-batch': 5, 'reset-mixed': 7}.get(case, 1)
            io = {'entered': case != 'fullqueue', 'calls': calls, 'events': events}
            gpr = [0] * 16
            if case in ('power-rep', 'power-rep-halt'): gpr[1], gpr[6] = 1, BASE + 0xf002
            state = [{'rip': hex(meta['expected_rip']), 'gpr': gpr}]
            memory = (work / 'expected-memory.bin').read_bytes()
            disk = (work / 'expected-disk.raw').read_bytes()
            cpu = {**state[0], 'stop': 'poweroff' if meta['power'] else 'reached', 'memory': [{'addr': hex(BASE), 'data': memory.hex()}]}
            cpus = [copy.deepcopy(cpu)]
            if case == 'power-rep-halt':
                peer_gpr = [0] * 16; peer_gpr[14], peer_gpr[15] = PEER_MARKER, PEER_R15
                state.append({'rip': hex(meta['peer_rip']), 'gpr': peer_gpr})
                cpus.append({**copy.deepcopy(state[1]), 'stop': 'cancelled', 'stats': {'halts': 1}, 'memory': copy.deepcopy(cpu['memory'])})
            report = {} if meta['sync_error'] else {'cpus': cpus, 'io_disk': io}
            n = meta['n']
            stderr = f'teardown: vcpus {n} joined {n} destroyed {n} vm_destroy 0 children none\n' + ('disk shutdown full-sync: injected\n' if meta['sync_error'] else '')
            args = [meta, report, state, io, stderr, 1 if meta['sync_error'] else 0, False, disk, disk, memory, [copy.deepcopy(cpu['memory']) for _ in range(n)]]
            assert not verify(*args), (case, verify(*args))
            if case in corrupt:
                bad = copy.deepcopy(args); bad[3]['events'] = corrupt[case](bad[3]['events'])
                if not meta['sync_error']: bad[1]['io_disk'] = bad[3]
                assert verify(*bad), case; controls += 1
            for index, mutate in ((2, lambda v: [dict(v[0], rip='0')] + v[1:]),
                                  (3, lambda v: dict(v, events=v['events'] + ['barrier-timeout'])),
                                  (3, lambda v: dict(v, events=v['events'] + ['trace-overflow'])),
                                  (4, lambda v: ''), (6, lambda v: True),
                                  (7, lambda v: bytes([v[0] ^ 1]) + v[1:])):
                bad = copy.deepcopy(args); bad[index] = mutate(bad[index])
                assert verify(*bad), (case, index); controls += 1
            bad = copy.deepcopy(args)
            bad[10][0][0]['data'] = ('00' if memory[0] else 'ff') + memory.hex()[2:]
            assert verify(*bad); controls += 1
            if not meta['sync_error']:
                bad = copy.deepcopy(args); bad[1]['cpus'][0]['memory'][0]['data'] = ('00' if memory[0] else 'ff') + memory.hex()[2:]
                assert verify(*bad); controls += 1
            if case == 'power-rep-halt':
                for index in (1, 2, 10):
                    for field in (('rip', 'r14', 'r15') if index in (1, 2) else ('memory',)):
                        bad = copy.deepcopy(args)
                        if index == 10: bad[10][1][0]['data'] = 'ff' + memory.hex()[2:]
                        else:
                            peer = bad[index]['cpus'][1] if index == 1 else bad[index][1]
                            if field == 'rip': peer['rip'] = '0'
                            else: peer['gpr'][int(field[1:])] = 0
                        assert verify(*bad); controls += 1
                for mutate in ('stop', 'reason', 'halts', 'trace', 'memory'):
                    bad = copy.deepcopy(args)
                    if mutate == 'stop': bad[1]['cpus'][1]['stop'] = 'reached'
                    elif mutate == 'halts': bad[1]['cpus'][1]['stats']['halts'] = 0
                    elif mutate == 'reason': bad[1]['cpus'][1].update(stop='resident', reason='exception status=1 detail=0')
                    elif mutate == 'memory': bad[1]['cpus'][1]['memory'][0]['data'] = 'ff' + memory.hex()[2:]
                    else: bad[3]['events'].remove('peer-halted')
                    assert verify(*bad); controls += 1
    print(f'PASS: assembled {len(CASES)} guests; {controls} negative oracle controls')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('self-check')
    prepare = sub.add_parser('prepare'); prepare.add_argument('--out', required=True, type=pathlib.Path)
    run = sub.add_parser('run')
    for key in ('host', 'runtime', 'fexc', 'out'): run.add_argument('--' + key, type=pathlib.Path, required=True)
    run.add_argument('--lock', type=pathlib.Path, default=VM_LOCK)
    run.add_argument('--cases', nargs='+', choices=CASES, default=list(CASES))
    args = parser.parse_args()
    if args.action == 'self-check': self_check(); return 0
    args.out = args.out.resolve(); args.out.mkdir(parents=True, exist_ok=False)
    if args.action == 'prepare':
        for case in CASES: fixture(case, args.out / case)
        return 0
    if len(set(args.cases)) != len(args.cases): parser.error('duplicate cases are not allowed')
    for key in ('host', 'runtime', 'fexc'): setattr(args, key, getattr(args, key).resolve(strict=True))
    sources = [p for p in (ROOT / 'crates').rglob('*') if p.is_file()]
    sources += [ROOT / p for p in ('Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'tools/run_virtio_lifecycle.py', 'tools/run_io_windows.py', 'tools/reference/ref.py', 'tools/reference/flags.py')]
    manifest = {'purpose': 'deterministic lifecycle integration, not performance evidence', 'source_root': str(ROOT),
                'source_sha256': {str(p.relative_to(ROOT)): digest(p) for p in sorted(sources)},
                'artifacts': {key: {'path': str(getattr(args, key)), 'sha256': digest(getattr(args, key))} for key in ('host', 'runtime', 'fexc')},
                'limits': {'vm_launches': len(args.cases), 'seconds_per_vm': 120, 'cpus_by_case': {case: 2 if case == 'power-rep-halt' else 1 for case in args.cases}},
                'excluded': ['SMP reset and running power peers', 'real stalled OS syscall', 'performance', 'malicious concurrent descriptor mutation']}
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
