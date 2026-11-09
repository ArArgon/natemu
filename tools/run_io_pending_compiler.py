#!/usr/bin/env python3
"""One bounded N=2 posted-compiler-response/window integration case.

The real unmodified compiler receives each request; only host response consumption
is held. This does not assert the compiler process remains busy. All guest RAM
observations use production copy windows or the final post-VM driver report.
"""
import argparse
import copy
import fcntl
import hashlib
import json
import pathlib
import struct
import subprocess
import tempfile
import time

from run_io_windows import ROOT, digest, stop_group, ref
from io_common import VM_LOCK
BASE, CODE, FUNCTION, STACK = 0x200000, 0x100000, 0x110000, 0x300000
TARGETS = (0x120000, 0x130000)


def fixture(work):
    data = bytearray(0x308); data[0x30] = 0xa5; data[0x3c:0x44] = b'\x55' * 8
    lines = ['identity', f'body {FUNCTION:#x} b801000000c3', f'memory {BASE:#x} {data.hex()}']
    for stage, target in enumerate(TARGETS):
        asm = f'mov byte ptr [rdi + {0x20 + stage:#x}], 1\nmov r10, {0xabcde001 + stage:#x}\nret\n'
        (work / f'target-{stage}.asm').write_text('.intel_syntax noprefix\n' + asm)
        lines.append(f'body {target:#x} {ref.assemble(asm).hex()}')
    ends = []
    for v in range(2):
        asm = f'''mov rdi, {BASE:#x}
again:
    lock not qword ptr [rdi + 0x3c]
    inc qword ptr [rdi + {0x100 + v * 8:#x}]
    mov rax, {FUNCTION:#x}
    call rax
    mov qword ptr [rdi + {0x200 + v * 8:#x}], rax
'''
        if v == 0:
            asm += f'''cmp byte ptr [rdi], 1
    jne phase_two
    cmp byte ptr [rdi + 0x20], 0
    jne finished_phase
    mov rax, {TARGETS[0]:#x}
    call rax
    jmp finished_phase
phase_two:
    cmp byte ptr [rdi], 2
    jne finished_phase
    cmp byte ptr [rdi + 0x21], 0
    jne finished_phase
    mov rax, {TARGETS[1]:#x}
    call rax
finished_phase:
'''
        asm += 'cmp byte ptr [rdi + 8], 0\nje again\nnop\n'
        (work / f'cpu-{v}.asm').write_text('.intel_syntax noprefix\n' + asm)
        code = ref.assemble(asm); ends.append(CODE + v * 0x1000 + len(code))
        lines += [f'body {CODE + v * 0x1000:#x} {code.hex()}', f'region {STACK + v * 0x1000:#x} 0x1000']
    for v in range(2):
        regs = [0] * 16; regs[4] = STACK + v * 0x1000 + 0xff0
        lines += ['cpu', f'rip {CODE + v * 0x1000:#x}', 'gpr ' + ' '.join(hex(r) for r in regs), 'cpl 0', 'rflags 0x2', 'if 0']
    (work / 'program.txt').write_text('\n'.join(lines) + '\n')
    meta = {'ends': ends, 'targets': list(TARGETS), 'initial_ram_sha256': hashlib.sha256(data).hexdigest()}
    (work / 'fixture.json').write_text(json.dumps(meta, indent=2) + '\n')
    return meta


def records(stderr):
    result = []
    for prefix in ('io-state: ', 'io-memory: ', 'io-pending: '):
        entries = [json.loads(line[len(prefix):]) for line in stderr.splitlines() if line.startswith(prefix)]
        if len(entries) != 1: raise ValueError('missing or duplicate final record')
        result.append(entries[0])
    return result


def judge(meta, report, states, memories, pending, stderr, exitcode, timeout):
    errors = []
    def require(ok, reason):
        if not ok: errors.append(reason)
    require(exitcode == 0 and not timeout, 'VM failed/timed out')
    require('teardown: vcpus 2 joined 2 destroyed 2 vm_destroy 0 children none' in stderr.splitlines(), 'unclean teardown')
    require(report['io_pending'] == pending and pending['posted'] == [True, True], 'missing posted requests or report mismatch')
    events = pending['events']; times = [e['ns'] for e in events]
    require(times == sorted(times), 'event time reversed')
    allowed = {'armed', 'posted-held', 'pending-window', 'held-recheck', 'window-result', 'released-consumer', 'resume-consumption', 'posted-after-release', 'timeout-safe-read', 'complete'}
    require(len(events) < 128 and all(e['event'] in allowed for e in events), 'unknown or truncated event trace')
    def one(name, stage=None):
        found = [(i, e) for i, e in enumerate(events) if e['event'] == name and (stage is None or e.get('stage') == stage)]
        if len(found) != 1: raise ValueError(f'expected one {name} stage {stage}')
        return found[0]
    epochs, posted = [], []
    for stage in range(2):
        arm_i, _ = one('armed', stage); post_i, post = one('posted-held', stage)
        pending_i, held = one('pending-window', stage); result_i, result = one('window-result', stage)
        release_i, _ = one('released-consumer', stage); resume_i, _ = one('resume-consumption', stage)
        require(arm_i < post_i < pending_i < release_i < resume_i, f'stage {stage} hold ordering')
        require(post['rip'] == TARGETS[stage] and post['pid'] > 0 and post['count'] >= 0, 'wrong posted request identity')
        posted.append(post)
        prior, status, final = held['prior'], held['status'], result['status']
        epoch = status['request']; epochs.append(epoch)
        require(epoch == status['request_after'] and epoch > prior['request'] and status['released'] < epoch and status['released_after'] < epoch and held['copy_pending']
                and status['write_windows'] == prior['write_windows'] and len(status['acknowledged']) == 2
                and status['acknowledged'][0] != epoch and status['acknowledged'][1] == epoch, 'no positive held epoch/peer ack evidence')
        require(final['request'] == final['request_after'] == final['released'] == final['released_after'] == epoch, 'wrong final epoch/release')
        if stage == 0:
            require(pending_i < result_i < release_i and not result['ok'] and result['ack_timeout'], 'not an acknowledgement timeout under hold')
            require(final['write_windows'] == prior['write_windows'] and final['acknowledged'][0] != epoch and final['acknowledged'][1] == epoch, 'timeout wrote or forged held acknowledgement')
        else:
            require(resume_i < result_i and result['ok'] and not result['ack_timeout'] and result['marker'] in (0, 1), 'grant/marker disposition invalid')
            require(final['write_windows'] == prior['write_windows'] + 1 and final['acknowledged'] == [epoch, epoch], 'successful copy lacks exact acknowledgements')
            recheck_i, recheck = one('held-recheck', stage)
            checked = recheck['status']
            require(pending_i < recheck_i < release_i and recheck['ns'] - held['ns'] >= 100_000_000, 'positive hold shorter than configured bound')
            require(checked['request'] == checked['request_after'] == epoch and checked['released'] < epoch
                    and checked['released_after'] < epoch and checked['write_windows'] == prior['write_windows']
                    and checked['acknowledged'][0] != epoch and checked['acknowledged'][1] == epoch
                    and recheck['copy_pending'], 'second observation no longer proves held pending copy')
    require(epochs[1] > epochs[0] and posted[1]['count'] > posted[0]['count'] and posted[1]['pid'] == posted[0]['pid'], 'fresh second request/epoch missing')
    safe_i, safe = one('timeout-safe-read'); done_i, done = one('complete')
    require(one('resume-consumption', 0)[0] < safe_i < one('armed', 1)[0] and done_i > one('window-result', 1)[0], 'safe-read/final progress ordering')
    require(safe['sentinel'] == 0xa5 and safe['function'] == 1, 'timeout had a RAM effect')
    require(done['snapshots'] == 64 and done['code'] == [2, 2] and done['markers'] == [1, 1]
            and len(done['before']) == len(done['after']) == 2 and all(a > b for a, b in zip(done['after'], done['before'])), 'missing post-release code/marker/progress')
    require(len(report['cpus']) == len(states) == len(memories) == 2, 'wrong CPU/memory count')
    all_images = []
    for v, cpu in enumerate(report['cpus']):
        require(cpu['stop'] == 'reached' and cpu['stats']['splits'] > 0, 'CPU did not complete split-atomic loop')
        require(ref.num(cpu['rip']) == ref.num(states[v]['rip']) == meta['ends'][v], 'CPU end RIP mismatch')
        for images in (cpu['memory'], memories[v]):
            require(len(images) == 1 and ref.num(images[0]['addr']) == BASE, 'wrong final memory region')
            data = bytes.fromhex(images[0]['data']); all_images.append(data)
            require(len(data) == 0x308, 'wrong final memory size')
            word = lambda at: struct.unpack_from('<Q', data, at)[0]
            expected = bytearray(0x308); expected[0], expected[8], expected[0x20], expected[0x21], expected[0x30] = 2, 1, 1, 1, 0x5a
            require(word(0x3c) in (0x5555555555555555, 0xaaaaaaaaaaaaaaaa), 'torn final atomic')
            expected[0x3c:0x44] = data[0x3c:0x44]
            for i in range(2):
                require(word(0x100 + i * 8) >= done['after'][i] and word(0x200 + i * 8) == 2, 'final code/counter mismatch')
                expected[0x100 + i * 8:0x108 + i * 8] = data[0x100 + i * 8:0x108 + i * 8]
                struct.pack_into('<Q', expected, 0x200 + i * 8, 2)
            require(data == expected, 'unexpected final RAM mutation')
    require(all(image == all_images[0] for image in all_images), 'CPU snapshots disagree')
    return errors


def self_check():
    with tempfile.TemporaryDirectory() as tmp:
        meta = fixture(pathlib.Path(tmp))
    events = []; clock = 0
    def add(name, **fields):
        nonlocal clock
        clock += 110_000_000; events.append({'event': name, 'ns': clock, **fields})
    def status(epoch, release, written, acks): return {'request': epoch, 'request_after': epoch, 'released': release, 'released_after': release, 'write_windows': written, 'acknowledged': acks}
    for stage in range(2):
        epoch = 2 + 4 * stage
        add('armed', stage=stage); add('posted-held', stage=stage, rip=TARGETS[stage], pid=100, count=10 + stage)
        add('pending-window', stage=stage, copy_pending=True, prior=status(epoch - 1, epoch - 1, epoch - 1, [epoch - 1] * 2), status=status(epoch, epoch - 1, epoch - 1, [epoch - 1, epoch]))
        if stage == 0:
            add('window-result', stage=stage, ok=False, ack_timeout=True, marker=0, status=status(epoch, epoch, epoch - 1, [epoch - 1, epoch]))
        if stage == 1: add('held-recheck', stage=stage, copy_pending=True, status=status(epoch, epoch - 1, epoch - 1, [epoch - 1, epoch]))
        add('released-consumer', stage=stage); add('resume-consumption', stage=stage)
        if stage == 0: add('timeout-safe-read', sentinel=165, function=1)
        else: add('window-result', stage=stage, ok=True, ack_timeout=False, marker=0, status=status(epoch, epoch, epoch, [epoch, epoch]))
    add('complete', snapshots=64, before=[1, 1], after=[2, 2], code=[2, 2], markers=[1, 1])
    pending = {'posted': [True, True], 'events': events}
    memory = bytearray(0x308); memory[0], memory[8], memory[0x20], memory[0x21], memory[0x30] = 2, 1, 1, 1, 0x5a
    memory[0x3c:0x44] = b'\x55' * 8
    for at in (0x100, 0x108, 0x200, 0x208): struct.pack_into('<Q', memory, at, 2)
    image = [{'addr': hex(BASE), 'data': memory.hex()}]
    states = [{'rip': hex(end)} for end in meta['ends']]
    report = {'io_pending': copy.deepcopy(pending), 'cpus': [{**state, 'stop': 'reached', 'stats': {'splits': 1}, 'memory': copy.deepcopy(image)} for state in states]}
    args = [meta, report, states, [copy.deepcopy(image), copy.deepcopy(image)], pending, 'teardown: vcpus 2 joined 2 destroyed 2 vm_destroy 0 children none\n', 0, False]
    assert not judge(*args), judge(*args)
    accepted = copy.deepcopy(args); accepted[4]['events'][-2]['marker'] = 1; accepted[1]['io_pending'] = copy.deepcopy(accepted[4]); assert not judge(*accepted)
    controls = 0
    for mutation in ('missing', 'duplicate', 'epoch', 'peer', 'held', 'released', 'written', 'ack-timeout', 'early-marker', 'final-marker', 'pid', 'no-progress', 'memory', 'cpu-rip', 'timeout', 'released-after', 'worker-finished', 'recheck-released', 'recheck-written', 'recheck-ack', 'recheck-worker', 'recheck-epoch'):
        bad = copy.deepcopy(args); e = bad[4]['events']
        if mutation == 'missing': e.pop(1)
        elif mutation == 'duplicate': e.insert(2, copy.deepcopy(e[1]))
        elif mutation == 'epoch': e[2]['status']['request_after'] += 1
        elif mutation == 'peer': e[2]['status']['acknowledged'][1] = 0
        elif mutation == 'held': e[2]['status']['acknowledged'][0] = 2
        elif mutation == 'released': e[2]['status']['released'] = 2
        elif mutation == 'written': e[3]['status']['write_windows'] = 2
        elif mutation == 'ack-timeout': e[3]['ack_timeout'] = False
        elif mutation == 'early-marker': e[-2]['marker'] = 2
        elif mutation == 'final-marker': e[-1]['markers'] = [1, 0]
        elif mutation == 'pid': e[8]['pid'] = 0
        elif mutation == 'no-progress': e[-1]['after'] = [1, 2]
        elif mutation == 'memory': bad[3][1][0]['data'] = 'ff' + memory.hex()[2:]
        elif mutation == 'cpu-rip': bad[2][0]['rip'] = '0'
        elif mutation == 'released-after': e[2]['status']['released_after'] = 2
        elif mutation == 'worker-finished': e[2]['copy_pending'] = False
        elif mutation.startswith('recheck-'):
            recheck = next(event for event in e if event['event'] == 'held-recheck')
            if mutation == 'recheck-released': recheck['status']['released_after'] = 6
            elif mutation == 'recheck-written': recheck['status']['write_windows'] = 6
            elif mutation == 'recheck-ack': recheck['status']['acknowledged'][0] = 6
            elif mutation == 'recheck-worker': recheck['copy_pending'] = False
            else: recheck['status']['request_after'] = 7
        else: bad[7] = True
        bad[1]['io_pending'] = copy.deepcopy(bad[4])
        try: errors = judge(*bad)
        except (ValueError, KeyError, IndexError): errors = ['invalid report']
        assert errors, mutation; controls += 1
    print(f'PASS: two-CPU guest assembled; both legal marker boundaries accepted; {controls} rejected mutants')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True); sub.add_parser('self-check')
    prepare = sub.add_parser('prepare'); prepare.add_argument('--out', type=pathlib.Path, required=True)
    run = sub.add_parser('run')
    for key in ('host', 'runtime', 'fexc', 'out'): run.add_argument('--' + key, type=pathlib.Path, required=True)
    run.add_argument('--lock', type=pathlib.Path, default=VM_LOCK)
    args = parser.parse_args()
    if args.action == 'self-check': self_check(); return 0
    args.out = args.out.resolve(); args.out.mkdir(parents=True, exist_ok=False); meta = fixture(args.out)
    if args.action == 'prepare': return 0
    for key in ('host', 'runtime', 'fexc'): setattr(args, key, getattr(args, key).resolve(strict=True))
    sources = [p for p in (ROOT / 'crates').rglob('*') if p.is_file()]
    sources += [ROOT / p for p in ('Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'tools/run_io_pending_compiler.py', 'tools/run_io_windows.py', 'tools/reference/ref.py', 'tools/reference/flags.py')]
    manifest = {'purpose': 'posted real compiler request; response consumption held, not compiler busy-time evidence', 'source_root': str(ROOT),
                'source_sha256': {str(p.relative_to(ROOT)): digest(p) for p in sorted(sources)},
                'artifacts': {key: {'path': str(getattr(args, key)), 'sha256': digest(getattr(args, key))} for key in ('host', 'runtime', 'fexc')},
                'limits': {'vm_launches': 1, 'seconds_per_vm': 120, 'cpus': 2, 'retained_bytes': 64 * 1024 * 1024}}
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    command = [str(args.host), str(args.runtime), 'run-program', str(args.fexc), str(args.out / 'program.txt'), str(args.out),
               '--watchdog-ms', '60000', '--compile-deadline-ms', '10000', '--ram', '0x4000000', '--code-shape', '--io-pending']
    (args.out / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    started, timeout = time.monotonic(), False
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (args.out / 'stdout.log').open('wb') as stdout, (args.out / 'stderr.log').open('wb') as stderr:
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
            try: process.wait(timeout=120)
            except subprocess.TimeoutExpired: timeout = True; stop_group(process)
            except BaseException: stop_group(process); raise
    stderr = (args.out / 'stderr.log').read_text(errors='replace')
    result = {'returncode': process.returncode, 'timeout': timeout, 'seconds': time.monotonic() - started}
    try:
        state, memory, pending = records(stderr); result['io_pending'] = pending
        report = json.loads((args.out / 'stdout.log').read_text())
        result['errors'] = judge(meta, report, state, memory, pending, stderr, process.returncode, timeout)
    except (ValueError, KeyError, TypeError, IndexError, struct.error) as error: result['errors'] = [f'invalid report: {error}']
    result['pass'] = not result['errors']; (args.out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2)); return 0 if result['pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
