#!/usr/bin/env python3
"""Forced N2 HLT quiescence across table/code writes and a later read-only grant.

prepare-only/self-check assemble and inspect fixtures; never build or launch a VM.
A run is one120s VM. A separately frozen stale-table resident mutant must fail
this same functional oracle; expected-negative disposition belongs to its caller.
"""
import argparse
import copy
import fcntl
import json
from pathlib import Path
import subprocess
import tempfile
import time

import run_io_windows as windows

ROOT = Path(__file__).resolve().parent.parent
A, B = 0x1122334455667788, 0x8877665544332211
BASE, FUNCTION, PTE = 0x200000, 0x110000, 0x503000
MARKER = 0x7175696574000000


def word(b, i, n): b[i:i + 8] = n.to_bytes(8, 'little')


def fixture(work):
    pml4, pdpt, pd, pt, apic = [bytearray(4096) for _ in range(5)]
    word(pml4, 0, 0x501003); word(pdpt, 0, 0x502003); word(pdpt, 3 * 8, 0x504003)
    word(pd, 0, PTE | 3); word(pd, 8, 0x200083); word(pd, 16, 0x400083)
    for i in range(512): word(pt, i * 8, i * 4096 | 3)
    word(pt, 0, 0x1000000 | 0x103)  # Global data route, independent of patched code page.
    word(apic, ((0xfee00000 >> 21) & 511) * 8, 0xfee00083)
    idt = bytearray(4096); target = 0x120000
    word(idt, 0x40 * 16, (target & 0xffff) | 8 << 16 | 0x8e << 40 | (target >> 16 & 0xffff) << 48)
    word(idt, 0x40 * 16 + 8, target >> 32)
    memories = [(BASE, bytes(64)), (0x400000, idt), (0x401000, (4095).to_bytes(2, 'little') + (0x400000).to_bytes(8, 'little')),
                (0x500000, pml4), (0x501000, pdpt), (0x502000, pd), (PTE, pt), (0x504000, apic),
                (0x1000000, A.to_bytes(8, 'little')), (0x1010000, B.to_bytes(8, 'little'))]
    handler = '''push rax
inc r10
mov eax, 0xfee000b0
mov dword ptr [rax], 0
pop rax
iretq
'''
    (work / 'handler.asm').write_text('.intel_syntax noprefix\n' + handler)
    lines = ['identity', f'body {FUNCTION:#x} b801000000c3', f'body {target:#x} {windows.ref.assemble(handler).hex()}',
             'mmio 0xfee00000 0xfee00000 unassigned']
    lines += [f'memory {at:#x} {bytes(data).hex()}' for at, data in memories]
    ends = []
    for v in range(2):
        assembly = f'''mov rsp, {0x300ff0 + v * 0x1000:#x}
mov rdi, {BASE + v * 32:#x}
lidt [0x401000]
mov rax, cr4
or eax, 0x80
mov cr4, rax
mov eax, 0x500000
mov cr3, rax
xor eax, eax
mov r14, [rax]
mov eax, {FUNCTION:#x}
call rax
mov r12, rax
mov r15, {MARKER + v:#x}
mov [rdi], r14
mov [rdi + 8], r12
mov [rdi + 16], r15
sti
hlt
xor eax, eax
mov r13, [rax]
mov eax, {FUNCTION:#x}
call rax
mov r11, rax
nop
'''
        (work / f'cpu-{v}.asm').write_text('.intel_syntax noprefix\n' + assembly)
        code = windows.ref.assemble(assembly); start = 0x100000 + v * 0x1000; ends.append(start + len(code))
        lines += [f'body {start:#x} {code.hex()}', f'region {0x300000 + v * 0x1000:#x} 0x1000']
    for v in range(2): lines += ['cpu', f'rip {0x100000 + v * 0x1000:#x}', 'cpl 0', 'rflags 0x2', 'if 0', 'svr 0x1ff']
    (work / 'program.txt').write_text('\n'.join(lines) + '\n')
    config = {'cpus': 2, 'ram': 0x4000000, 'global_va': 0, 'old_physical': 0x1000000, 'new_physical': 0x1010000,
              'pte': PTE, 'cached_function': FUNCTION, 'warm_proof': BASE, 'ends': ends, 'wake_vector': 0x40}
    (work / 'fixture.json').write_text(json.dumps(config, indent=2) + '\n'); return config


def judge(report, config):
    errors = []
    def need(ok, why):
        if not ok: errors.append(why)
    cpus, io = report['cpus'], report['io_quiet']
    need(len(cpus) == 2, 'two participants')
    for v, cpu in enumerate(cpus):
        need(cpu['stop'] == 'reached' and int(cpu['rip'], 0) == config['ends'][v], f'CPU{v} clean end')
        registers = [int(n, 0) if isinstance(n, str) else n for n in cpu['gpr']]
        for r, value in ((14, A), (12, 1), (15, MARKER + v), (13, B), (11, 2), (10, 1)):
            need(registers[r] == value, f'CPU{v} r{r} expected {value:#x}, got {registers[r]:#x}')
        need(cpu['stats']['halts'] > 0 and cpu['stats']['interrupts'] > 0, f'CPU{v} HLT and IRQ delivery')
        need(cpu['frame_intact'] and not cpu['tampered'], f'CPU{v} frame/tamper')
    warm = bytearray(64)
    for v in range(2):
        word(warm, v * 32, A); word(warm, v * 32 + 8, 1); word(warm, v * 32 + 16, MARKER + v)
    need(io['warm'] == list(warm), 'both CPUs warmed old global mapping and compiled function')
    need(io['windows'] == 3 and io['cancel_prevented_grant'], 'three grants and pre-canceled zero-effect request')
    events = io['events']; need([e['stage'] for e in events] == ['quiet', 'warm-read', 'write', 'read-after-write', 'canceled', 'ended'], 'complete ordered host observations')
    states = [e['status'] for e in events]
    first = states[0]; base = first['request']; acks = first['acknowledged']
    need(len(acks) == 2 and all(a <= base for a in acks), 'initial ACK tuple')
    for i, s in enumerate(states):
        epoch = base + min(i, 3)
        need(s['request'] == s['request_after'] == s['released'] == s['released_after'] == epoch, f'stage{i} completed epoch')
        need(s['written'] == (first['written'] if i < 2 else base + 2), f'stage{i} write epoch retained across later read/cancel')
        need(s['quiescent'] == ([0, 0] if i == 5 else [1, 1]), f'stage{i} actual quiet flags')
        if i < 5: need(s['acknowledged'] == acks, f'stage{i} no synthesized or resident quiet ACK')
        else: need(s['acknowledged'] == [epoch, epoch], 'both resumed CPUs acknowledged final epoch')
    return errors


def self_check():
    with tempfile.TemporaryDirectory() as d:
        config = fixture(Path(d))
        warm = bytearray(64)
        for v in range(2): word(warm, v * 32, A); word(warm, v * 32 + 8, 1); word(warm, v * 32 + 16, MARKER + v)
        stages = ['quiet', 'warm-read', 'write', 'read-after-write', 'canceled', 'ended']
        events = [{'stage': stage, 'status': {'request': min(i, 3), 'request_after': min(i, 3), 'released': min(i, 3), 'released_after': min(i, 3),
                    'written': 0 if i < 2 else 2, 'acknowledged': [3, 3] if i == 5 else [0, 0], 'quiescent': [0, 0] if i == 5 else [1, 1]}} for i, stage in enumerate(stages)]
        cpus = []
        for v in range(2):
            regs = [0] * 16
            for r, value in ((14, A), (12, 1), (15, MARKER + v), (13, B), (11, 2), (10, 1)): regs[r] = value
            cpus.append({'stop': 'reached', 'rip': hex(config['ends'][v]), 'gpr': regs, 'stats': {'halts': 1, 'interrupts': 1}, 'frame_intact': True, 'tampered': False})
        good = {'cpus': cpus, 'io_quiet': {'windows': 3, 'cancel_prevented_grant': True, 'warm': list(warm), 'events': events}}
        assert not judge(good, config), judge(good, config)
        bad = copy.deepcopy(good); bad['io_quiet']['events'][-1]['status']['acknowledged'] = [0, 0]; assert judge(bad, config)
        for field, value in [('acknowledged', [1, 0]), ('quiescent', [0, 1]), ('released_after', 0), ('written', 3)]:
            bad = copy.deepcopy(good); bad['io_quiet']['events'][3]['status'][field] = value; assert judge(bad, config)
        for r, value in ((14, B), (12, 2), (13, A), (11, 1), (10, 0)):
            bad = copy.deepcopy(good); bad['cpus'][0]['gpr'][r] = value; assert judge(bad, config)
        bad = copy.deepcopy(good)
        for cpu in bad['cpus']: cpu['gpr'][13] = A
        failures = judge(bad, config)
        assert len(failures) == 2 and all('r13 expected' in e for e in failures), failures
    print('Fixture assembly and quiet/currency/code/IRQ oracle controls pass; no build/VM')


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--self-check', action='store_true'); p.add_argument('--prepare-only', action='store_true')
    for n in ('host', 'runtime', 'fexc', 'out'): p.add_argument('--' + n, type=Path)
    p.add_argument('--lock', type=Path, default=Path.home() / 'orca/natemu-raw/.vm.lock')
    args = p.parse_args()
    if args.self_check: self_check(); return 0
    if args.out is None: p.error('--out required')
    if not args.prepare_only and any(getattr(args, n) is None for n in ('host', 'runtime', 'fexc')): p.error('run requires host/runtime/fexc')
    args.out = args.out.resolve(); args.out.mkdir(parents=True, exist_ok=False); config = fixture(args.out)
    sources = [ROOT / 'crates/driver/src/drivers/program.rs', ROOT / 'crates/driver/src/drivers/program_quiet.rs', Path(__file__), Path(windows.__file__), ROOT / 'tools/reference/ref.py', ROOT / 'tools/reference/flags.py']
    manifest = {'purpose': 'forced HLT quiet-grant correctness; independent review required', 'source_root': str(ROOT),
                'sources': {str(p): windows.digest(p) for p in sources},
                'limits': {'vm_launches': 0 if args.prepare_only else 1, 'seconds_per_vm': 120}, 'artifacts': {}}
    for n in ('host', 'runtime', 'fexc'):
        if getattr(args, n) is not None:
            value = getattr(args, n).resolve(strict=True); setattr(args, n, value); manifest['artifacts'][n] = {'path': str(value), 'sha256': windows.digest(value)}
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    if args.prepare_only: return 0
    command = [str(args.host), str(args.runtime), 'run-program', str(args.fexc), str(args.out / 'program.txt'), str(args.out), '--io-quiet', '--watchdog-ms', '60000', '--ram', hex(config['ram']), '--code-shape']
    (args.out / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    start = time.monotonic(); timeout = False
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (args.out / 'stdout.log').open('wb') as out, (args.out / 'stderr.log').open('wb') as err:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
            try: process.wait(timeout=120)
            except subprocess.TimeoutExpired: timeout = True; windows.stop_group(process)
            except BaseException: windows.stop_group(process); raise
    errors = []
    if timeout or process.returncode: errors.append('process/timeout failure')
    text = (args.out / 'stderr.log').read_text(errors='replace')
    if 'teardown: vcpus 2 joined 2 destroyed 2 vm_destroy 0 children none' not in text.splitlines(): errors.append('clean teardown absent')
    report = None
    try:
        report = json.loads((args.out / 'stdout.log').read_text()); errors += judge(report, config)
    except (ValueError, KeyError, IndexError, TypeError) as e: errors.append('invalid report: ' + str(e))
    result = {'pass': not errors, 'errors': errors, 'returncode': process.returncode, 'timeout': timeout, 'seconds': time.monotonic() - start,
              'io_quiet': report.get('io_quiet') if isinstance(report, dict) else None}
    (args.out / 'result.json').write_text(json.dumps(result, indent=2) + '\n'); print(json.dumps(result), flush=True)
    return int(bool(errors))


if __name__ == '__main__': raise SystemExit(main())
