#!/usr/bin/env python3
"""Internal 64-vCPU x2APIC routing checks; no public test changes.

tools/with-env python3 tools/run_capacity_apic.py --host HOST --runtime RUNTIME --fexc FEXC --out NEW_DIR
tools/with-env python3 tools/run_capacity_apic.py --self-check

Physical ring, physical broadcast and shorthand all/all-excluding-self, then an
HLT ring. Every CPU must finish with exact vector counts and its own APIC ID.
Runs default and one-instruction blocks. Full raw output survives failures/caps.
This is a bounded internal architectural oracle, not a QEMU/native comparison;
the HLT case requires observed HLT activity, not proof every CPU actually parked.
"""
import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools/bench/smp')]
import programs  # noqa: E402
import events  # noqa: E402
import run_slice3_vm as s3  # noqa: E402
import run_slice1_vm as vm  # noqa: E402

NCPU, ROUNDS = 64, 3
CODE, HANDLER, STATE, IDT, STACK = 0x1000000, 0x1800000, 0x2000000, 0x2800000, 0x3000000
STRIDE = 64
READY, DONE, IDS, COUNTS = STATE, STATE + 0x200, STATE + 0x400, STATE + 0x800
VECTORS = (0x50, 0x51, 0x52, 0x53)


def count(cpu, vector):
    return COUNTS + cpu * STRIDE + 8 * (vector - VECTORS[0])


def send(vector, destination=0, shorthand=0):
    return f'mov ecx, 0x830\nmov eax, {vector | shorthand}\nmov edx, {destination}\nwrmsr\n'


def barrier(base, label):
    return label + ':\npause\n' + ''.join(
        f'cmp qword [{base + 8 * cpu:#x}], 1\njne {label}\n' for cpu in range(NCPU))


def make_program(kind):
    handler_text = ''
    for vector in VECTORS:
        handler_text += f'''irq{vector}:
push rax
push rcx
push rdx
push rbx
mov ecx, 0x802
rdmsr
shl eax, 6
mov ebx, {COUNTS + 8 * (vector - VECTORS[0])}
inc qword [rbx + rax]
mov ecx, 0x80b
xor eax, eax
xor edx, edx
wrmsr
pop rbx
pop rdx
pop rcx
pop rax
iretq
'''
    code, labels = programs.ref_assemble(handler_text + 'handler_end:\nnop\n')
    gates = bytearray(4096)
    for vector in VECTORS:
        address = HANDLER + labels[f'irq{vector}']
        low = address & 0xffff | 8 << 16 | 0x8e << 40 | (address >> 16 & 0xffff) << 48
        gates[16 * vector:16 * vector + 16] = low.to_bytes(8, 'little') + (address >> 32).to_bytes(8, 'little')
    prog = dict(name=f'capacity-apic-{kind}', format='natemu-smp-v1', ncpu=NCPU, system=True,
                bodies=[dict(addr=hex(HANDLER), code=code.hex())], cpus=[], memory=[
                    dict(addr=hex(IDT), data=gates.hex()),
                    dict(addr=hex(IDT + 4096), data=(4095).to_bytes(2, 'little').hex() + IDT.to_bytes(8, 'little').hex()),
                    dict(addr=hex(STATE), size=0x2000, fill='zero')], params=dict(kind=kind, rounds=ROUNDS))
    for cpu in range(NCPU):
        text = f'''cli
lidt [{IDT + 4096:#x}]
mov ecx, 0x1b
rdmsr
or eax, 0xc00
wrmsr
mov ecx, 0x80f
mov eax, 0x1ff
xor edx, edx
wrmsr
mov ecx, 0x808
xor eax, eax
wrmsr
mov ecx, 0x802
rdmsr
mov [{IDS + 8 * cpu:#x}], rax
mov qword [{READY + 8 * cpu:#x}], 1
'''
        text += barrier(READY, 'ready')
        if kind in ('physical', 'halt'):
            text += 'mov r8d, 1\nround:\n'
            wait = f'wait_receipt:\ncli\ncmp qword [{count(cpu, 0x50):#x}], r8\njae received\nsti\n'
            wait += ('hlt\n' if kind == 'halt' else 'pause\n') + 'jmp wait_receipt\nreceived:\n'
            post = send(0x50, (cpu + 1) % NCPU)
            text += post + wait if cpu == 0 else wait + post
            text += f'inc r8d\ncmp r8d, {ROUNDS}\njbe round\n'
        elif kind == 'broadcast':
            text += 'sti\n'
            if cpu == 63:
                # Distinct vectors prevent legal IRR coalescing between phases.
                text += send(0x51, 0xffffffff) + send(0x52, shorthand=0x80000) + send(0x53, shorthand=0xc0000)
            text += 'receipts:\npause\n'
            for vector in (0x51, 0x52) + (() if cpu == 63 else (0x53,)):
                text += f'cmp qword [{count(cpu, vector):#x}], 1\njne receipts\n'
        else:
            raise ValueError(kind)
        text += f'mov qword [{DONE + 8 * cpu:#x}], 1\nsti\n' + barrier(DONE, 'done') + 'cli\ncmp r11, r11\n'
        body, _ = programs.ref_assemble(text)
        assert len(body) < 0x1000
        start, stack = CODE + cpu * 0x1000, STACK + (cpu + 1) * 0x1000
        prog['bodies'].append(dict(addr=hex(start), code=body.hex()))
        prog['cpus'].append(dict(programs.cpu(cpu, start, rsp=stack), cpl=0))
        prog['memory'].append(dict(addr=hex(stack - 0x1000), size=0x1000, fill='zero'))
    spans = [(int(b['addr'], 0), len(b['code']) // 2) for b in prog['bodies']]
    spans += [(int(m['addr'], 0), len(m['data']) // 2 if 'data' in m else m['size']) for m in prog['memory']]
    spans.sort()
    assert all(a + size <= b for (a, size), (b, _) in zip(spans, spans[1:])), 'fixture regions overlap'
    return prog


def expected(kind):
    if kind in ('physical', 'halt'):
        return [[ROUNDS, 0, 0, 0] for _ in range(NCPU)]
    return [[0, 1, 1, int(cpu != 63)] for cpu in range(NCPU)]


def check_state(kind, data, halts):
    read = lambda address: int.from_bytes(data[address - STATE:address - STATE + 8], 'little')
    problems = []
    assert len(data) == 0x2000
    actual = [[read(count(cpu, v)) for v in VECTORS] for cpu in range(NCPU)]
    for cpu, (got, want) in enumerate(zip(actual, expected(kind))):
        if got != want:
            problems.append(f'cpu{cpu} vector counts {got} != {want}')
        if read(READY + 8 * cpu) != 1 or read(DONE + 8 * cpu) != 1:
            problems.append(f'cpu{cpu} did not complete both barriers')
        if read(IDS + 8 * cpu) != cpu:
            problems.append(f'cpu{cpu} x2APIC ID {read(IDS + 8 * cpu)} != {cpu}')
    if kind == 'halt' and not any(halts):
        problems.append('no HLT activity observed')
    return problems, actual


def judge(prog, output):
    cpus = output['cpus']
    if len(cpus) != NCPU:
        return [f'{len(cpus)} CPU results, expected {NCPU}'], {}
    problems = []
    for cpu, result in enumerate(cpus):
        end = s3.body_end(prog, int(prog['cpus'][cpu]['rip'], 0))
        if result['stop'] != 'reached' or int(result['rip'], 0) != end:
            problems.append(f"cpu{cpu} stopped {result['stop']} at {result['rip']}, expected end {end:#x}")
        problems += [f'cpu{cpu}: {p}' for p in s3.per_vcpu_checks(result)]
    data = next(bytes.fromhex(m['data']) for m in cpus[0]['memory'] if int(m['addr'], 0) == STATE)
    halts = [c['stats']['halts'] for c in cpus]
    errors, counts = check_state(prog['params']['kind'], data, halts)
    return problems + errors, dict(counts=counts, halts=halts)


def self_check():
    for kind in ('physical', 'broadcast', 'halt'):
        make_program(kind)
        data = bytearray(0x2000)
        def put(address, value):
            data[address - STATE:address - STATE + 8] = value.to_bytes(8, 'little')
        for cpu, values in enumerate(expected(kind)):
            put(READY + 8 * cpu, 1); put(DONE + 8 * cpu, 1); put(IDS + 8 * cpu, cpu)
            for vector, value in zip(VECTORS, values):
                put(count(cpu, vector), value)
        assert not check_state(kind, data, [1] * NCPU)[0]
        for cpu in (15, 16, 31, 32, 47, 48, 63):
            address = count(cpu, 0x50 if kind != 'broadcast' else 0x52)
            old = int.from_bytes(data[address - STATE:address - STATE + 8], 'little')
            put(address, old + 1)
            assert check_state(kind, data, [1] * NCPU)[0], f'missed wrong count on CPU{cpu}'
            put(address, old)
        if kind == 'broadcast':
            put(count(63, 0x53), 1)
            assert check_state(kind, data, [1] * NCPU)[0], 'missed self-exclusion failure'
        if kind == 'halt':
            assert check_state(kind, data, [0] * NCPU)[0], 'accepted absent HLT coverage'
    print('Assembly/layout and exact-count oracle self-checks passed; no VMs launched.')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ('host', 'runtime', 'fexc', 'out'):
        parser.add_argument('--' + name, type=pathlib.Path)
    parser.add_argument('--self-check', action='store_true')
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return 0
    if not all(getattr(args, name) for name in ('host', 'runtime', 'fexc', 'out')):
        parser.error('--host, --runtime, --fexc and --out are required')
    args.out.mkdir(parents=True, exist_ok=False)
    pins = {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in (args.host, args.runtime, args.fexc, pathlib.Path(__file__))}
    report = dict(pins=pins, planned=6, results=[], limitations=[
        'Internal architectural oracle; no native/QEMU reference execution.',
        'HLT counts establish activity; they do not prove every CPU parked.',
        'No logical destination, timer, NMI, hotplug or high-RAM coverage.'])
    for kind in ('physical', 'broadcast', 'halt'):
        prog = make_program(kind)
        text = events.description(prog, [[] for _ in range(NCPU)])
        for limit in (None, 1):
            work = args.out / f'{kind}-{limit or "default"}'
            work.mkdir()
            (work / 'program.txt').write_text(text)
            cmd = [str(args.host.resolve()), str(args.runtime.resolve()), 'run-program', str(args.fexc.resolve()),
                   str((work / 'program.txt').resolve()), str(work.resolve()), *vm.frame_alias_args(),
                   '--watchdog-ms', '60000', *([str(limit)] if limit else [])]
            (work / 'command.json').write_text(json.dumps(cmd, indent=2) + '\n')
            start, problems, detail = time.monotonic(), [], {}
            with (work / 'stdout.json').open('w') as stdout, (work / 'stderr.txt').open('w') as stderr:
                try:
                    run = subprocess.run(cmd, stdout=stdout, stderr=stderr, timeout=300)
                    rc = run.returncode
                except subprocess.TimeoutExpired:
                    rc = 'timeout'
                except OSError as exc:
                    rc = 'launch-error'
                    problems.append(str(exc))
            if rc == 0:
                try:
                    problems, detail = judge(prog, json.loads((work / 'stdout.json').read_text()))
                except (KeyError, ValueError, TypeError, StopIteration, AssertionError) as exc:
                    problems.append(f'malformed/incomplete output: {exc}')
            else:
                problems.append(f'driver result {rc}; raw stdout/stderr retained')
            row = dict(kind=kind, max_block=limit, returncode=rc, seconds=time.monotonic() - start,
                       status='fail' if problems else 'pass', problems=problems, oracle=detail,
                       hashes={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in work.iterdir() if p.is_file()})
            report['results'].append(row)
            report['complete'] = len(report['results']) == report['planned']
            (args.out / 'capacity-apic.json').write_text(json.dumps(report, indent=2) + '\n')
            print(f'{kind} max-block={limit}: {row["status"]}', *problems[:5], sep='\n  ', flush=True)
            if rc == 'timeout':
                # Do not launch another VM after a cap: the orchestrator must check child cleanup first.
                return 1
    return int(any(r['status'] != 'pass' for r in report['results']))


if __name__ == '__main__':
    raise SystemExit(main())
