#!/usr/bin/env python3
"""Additional live window development cases using the unchanged program-driver worker.

Serial N=2 cases, each capped at 120 seconds; no builds, retries or VM launch
in --prepare-only mode. High RAM and PTE alias cases check concrete addressing /
translation outcomes. HLT and AP cases check native waiting plus successful window
interleavings, not which individual grant was acknowledged in a waiting state.
This script shares authorship with run_io_windows.py and is not independent evidence.
"""
import argparse
import fcntl
import json
import pathlib
import subprocess
import time

import run_io_windows as windows

ROOT = pathlib.Path(__file__).resolve().parent.parent
CASES = ('high', 'pte', 'hlt', 'ap', 'pte-code', 'pde-code')
A, B = 0x1122334455667788, 0x8877665544332211


def word(buf, index, value):
    buf[index:index + 8] = value.to_bytes(8, 'little')


def fixture(case, work):
    base, code, function, ram = windows.BASE, windows.CODE, windows.FUNCTION, 0x4000000
    stack = windows.STACK
    if case == 'high':
        base, code, function, ram = 0x100180000, 0x100100000, 0x100110000, 0xc0200000
    if case in ('pte', 'pte-code'): base = 0x200002
    if case == 'pde-code': base, code, function, stack = 0x502003, 0x400000, 0x410000, 0x600000
    data_at = base & ~0xfff
    data = bytearray(4096 if case in ('pte', 'pte-code', 'pde-code') else 0x308)
    data_offset = base - data_at
    data[data_offset + 0x3c:data_offset + 0x44] = b'\x55' * 8
    lines = ['identity', f'body {function:#x} b801000000c3']
    extra = []
    if case in ('pte', 'pte-code'):
        # PTE[0] initially points at 16 MiB. BASE addresses byte 2 of that same
        # live entry: the worker's final done byte changes the PFN by 64 KiB.
        # Ordinary fixture data occupies unused entries in this PT; code entries
        # at indices 256+ are outside the worker's read/write scratch extents.
        word(data, 0, 0x1000003 | (0x100 if case == 'pte-code' else 0))
        for address in (code, code + 0x1000, function):
            word(data, (address >> 12) * 8, address | 3)
        pml4, pdpt, pd = bytearray(4096), bytearray(4096), bytearray(4096)
        word(pml4, 0, 0x501003); word(pdpt, 0, 0x502003)
        word(pd, 0, data_at | 3)
        word(pd, 8, 0x200083); word(pd, 16, 0x400083)
        image = lambda value: (b'\x48\xb8' + value.to_bytes(8, 'little') + b'\xc3') if case == 'pte-code' else value.to_bytes(8, 'little')
        for address, image in ((0x500000, pml4), (0x501000, pdpt), (0x502000, pd),
                               (0x1000000, image(A)), (0x1010000, image(B))):
            lines.append(f'memory {address:#x} {image.hex()}')
    elif case == 'pde-code':
        # The final host write sets byte 3 of this global 2-MiB leaf from 2 to 1:
        # VA 0 moves from physical 32 MiB to 16 MiB. Main code/tables/stacks stay
        # identity-mapped through distinct leaves, outside the worker's scratch.
        word(data, 0, 0x2000183)
        word(data, 16, 0x400083); word(data, 24, 0x600083)
        pml4, pdpt = bytearray(4096), bytearray(4096)
        word(pml4, 0, 0x501003); word(pdpt, 0, data_at | 3)
        for address, image in ((0x500000, pml4), (0x501000, pdpt)):
            lines.append(f'memory {address:#x} {image.hex()}')
        for address, value in ((0x2000000, A), (0x1000000, B)):
            image = b'\x48\xb8' + value.to_bytes(8, 'little') + b'\xc3'
            lines += [f'memory {address:#x} {image.hex()}', f'memory {address + 0x1000:#x} {value.to_bytes(8, "little").hex()}']
    elif case == 'hlt':
        handler = '''push rax
    inc qword ptr [rdi + r15*8 + 0x280]
    mov eax, 0xfee000b0
    mov dword ptr [rax], 0
    pop rax
    iretq
'''
        (work / 'interrupt.asm').write_text('.intel_syntax noprefix\n' + handler)
        lines.append(f'body 0x120000 {windows.ref.assemble(handler).hex()}')
        idt = bytearray(4096)
        vector, target = 0x40, 0x120000
        word(idt, vector * 16, target & 0xffff | 8 << 16 | 0x8e << 40 | (target >> 16 & 0xffff) << 48)
        word(idt, vector * 16 + 8, target >> 32)
        lines += [f'memory 0x400000 {idt.hex()}',
                  'memory 0x401000 ' + ((4095).to_bytes(2, 'little') + (0x400000).to_bytes(8, 'little')).hex(),
                  'mmio 0xfee00000 0xfee00000 unassigned']
        extra = ['--kick-vcpu', '0', '--kick-vcpu', '1', '--kick-events', '40',
                 '--kicks', '100000', '--kick-every-us', '1000']
    elif case == 'ap':
        lines.append('memory 0x400000 ' + bytes(4096).hex())
    lines.append(f'memory {data_at:#x} {data.hex()}')
    for v in range(2):
        prelude = f'mov rsp, {stack + v * 0x1000 + 0xff0:#x}\nmov rdi, {base:#x}\n'
        tail = ''
        if case == 'pte':
            prelude += 'mov eax, 0x500000\nmov cr3, rax\nxor eax, eax\nmov r14, [rax]\n'
            tail = 'xor eax, eax\nmov r13, [rax]\n'
        elif case in ('pte-code', 'pde-code'):
            prelude += 'mov rax, cr4\nor eax, 0x80\nmov cr4, rax\nmov eax, 0x500000\nmov cr3, rax\nxor eax, eax\ncall rax\nmov r14, rax\n'
            tail = 'xor eax, eax\ncall rax\nmov r13, rax\n'
            if case == 'pde-code':
                prelude += 'mov r12, [0x1000]\n'
                tail += 'mov r11, [0x1000]\n'
        elif case == 'hlt':
            # IF remains clear until the real handler and its stack/registers exist.
            # Early kick events stay pending; the default logging IDT is never taken.
            prelude += f'mov r15, {v}\nlidt [0x401000]\nsti\n'
        elif case == 'ap' and v == 0:
            prelude += '''rdtsc
    shl rdx, 32
    or rax, rdx
    lea r10, [rax + 200000000]
wait_time:
    rdtsc
    shl rdx, 32
    or rax, rdx
    cmp rax, r10
    jb wait_time
    mov dword ptr [0x400004], 1
    mov eax, 0x101000
    mov qword ptr [0x400008], rax
    mov word ptr [0x400000], 1
wait_ack:
    cmp word ptr [0x400000], 0
    jne wait_ack
'''
        rewarm = ''
        if case in ('pte-code', 'pde-code'):
            # Publish the shared function's result only AFTER warming VA 0. Once
            # the host observes 2, this proves the global code mapping was used
            # after the preceding code-patch window, before the final leaf edit.
            rewarm = '    mov rbx, rax\n    xor eax, eax\n    call rax\n'
            if case == 'pde-code': rewarm += '    mov rax, [0x1000]\n'
            rewarm += '    mov rax, rbx\n'
        assembly = prelude + f'''again:
    lock not qword ptr [rdi + 0x3c]
    inc qword ptr [rdi + {0x100 + 8 * v:#x}]
    mov rax, {function:#x}
    call rax
''' + rewarm + f'''
    mov qword ptr [rdi + {0x200 + 8 * v:#x}], rax
    cmp byte ptr [rdi], {2 if case == 'pde-code' else 0}
    jne done
''' + ('    hlt\n' if case == 'hlt' else '') + '    jmp again\ndone:\n' + tail + '    nop\n'
        (work / f'cpu-{v}.asm').write_text('.intel_syntax noprefix\n' + assembly)
        lines += [f'body {code + v * 0x1000:#x} {windows.ref.assemble(assembly).hex()}',
                  f'region {stack + v * 0x1000:#x} 0x1000']
    for v in range(2):
        lines += ['cpu', f'rip {code + v * 0x1000:#x}', 'cpl 0', 'rflags 0x2', 'if 0']
        if case == 'hlt': lines.append('svr 0x1ff')
        if case == 'ap' and v == 1: lines.append('mailbox 0x400000')
    (work / 'program.txt').write_text('\n'.join(lines) + '\n')
    config = {'case': case, 'n': 2, 'ram': ram, 'base': base, 'code': code, 'function': function,
              'extra': extra, 'forced_waiting_grant_overlap': False}
    (work / 'fixture.json').write_text(json.dumps(config, indent=2) + '\n')
    return config


def judge(report, case):
    errors = []
    cpus, io = report['cpus'], report['io_windows']
    if len(cpus) != 2: errors.append('wrong participant count')
    for v, cpu in enumerate(cpus):
        if cpu['stop'] != 'reached': errors.append(f'CPU {v} did not reach end')
        if cpu['stats']['splits'] <= 0: errors.append(f'CPU {v} did not execute split atomics')
        if case in ('pte', 'pte-code', 'pde-code') and (int(cpu['gpr'][14], 0) != A or int(cpu['gpr'][13], 0) != B):
            errors.append(f'CPU {v} did not observe the PTE mapping change')
        if case == 'pde-code' and (int(cpu['gpr'][12], 0) != A or int(cpu['gpr'][11], 0) != B):
            errors.append(f'CPU {v} did not observe the large-page data mapping change')
        if case == 'hlt' and (cpu['stats']['halts'] == 0 or cpu['stats']['interrupts'] == 0):
            errors.append(f'CPU {v} did not halt and take an interrupt')
    if io['snapshots'] != 64 or io['windows'] < 68 or not io['ordered_overlap'] or io['code_observed'] != 2:
        errors.append('window protocol checks failed')
    if case in ('pte-code', 'pde-code'): errors.extend(windows.cache_preservation_errors(cpus))
    before, after = io['progress_before'], io['progress_after']
    if len(before) != 2 or len(after) != 2 or any(b <= a for a, b in zip(before, after)):
        errors.append('no progress between snapshots')
    if case == 'ap':
        mailbox = cpus[1]['stats']['mailbox']
        if mailbox[0] != 0 or mailbox[1] == 0 or mailbox[2] == 0:
            errors.append('AP did not wait and acknowledge its wakeup')
    return errors


def run(args, case, work, config):
    command = [str(args.host), str(args.runtime), 'run-program', str(args.fexc), str(work / 'program.txt'), str(work),
               '--watchdog-ms', '60000', '--ram', hex(config['ram']), '--code-shape',
               '--io-windows', hex(config['base']), '--io-window-code', hex(config['function']), *config['extra']]
    (work / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    started, timed_out = time.monotonic(), False
    with (work / 'stdout.log').open('wb') as stdout, (work / 'stderr.log').open('wb') as stderr:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
        try: process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            timed_out = True; windows.stop_group(process)
        except BaseException:
            windows.stop_group(process); raise
    errors = []
    if process.returncode != 0 or timed_out: errors.append('VM did not exit successfully')
    if 'teardown: vcpus 2 joined 2 destroyed 2 vm_destroy 0 children none' not in (work / 'stderr.log').read_text(errors='replace').splitlines():
        errors.append('missing clean teardown')
    report = None
    try:
        report = json.loads((work / 'stdout.log').read_text())
        errors += judge(report, case)
    except (ValueError, KeyError, TypeError, IndexError) as error:
        errors.append(f'invalid report: {error}')
    result = {'case': case, 'pass': not errors, 'errors': errors, 'returncode': process.returncode,
              'timeout': timed_out, 'seconds': time.monotonic() - started,
              'io_windows': report.get('io_windows') if isinstance(report, dict) else None}
    (work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(f"{case}: {'PASS' if result['pass'] else 'FAIL'} {errors}", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=pathlib.Path)
    parser.add_argument('--cases', nargs='+', choices=CASES, default=list(CASES))
    parser.add_argument('--prepare-only', action='store_true')
    for name in ('host', 'runtime', 'fexc'): parser.add_argument('--' + name, type=pathlib.Path)
    parser.add_argument('--lock', type=pathlib.Path, default=pathlib.Path.home() / 'orca/natemu-raw/.vm.lock')
    args = parser.parse_args()
    if len(set(args.cases)) != len(args.cases): parser.error('duplicate cases')
    if not args.prepare_only:
        for name in ('host', 'runtime', 'fexc'):
            if getattr(args, name) is None: parser.error('--host, --runtime and --fexc are required for VM runs')
            setattr(args, name, getattr(args, name).resolve(strict=True))
    args.out = args.out.resolve(); args.out.mkdir(parents=True, exist_ok=False)
    sources = [p for p in (ROOT / 'crates').rglob('*') if p.is_file()]
    sources += [ROOT / p for p in ('Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'tools/run_io_window_cases.py',
                                 'tools/run_io_windows.py', 'tools/reference/ref.py', 'tools/reference/flags.py')]
    manifest = {'purpose': 'development; shared fixture authorship, separate review required', 'source_root': str(ROOT),
                'source_sha256': {str(p.relative_to(ROOT)): windows.digest(p) for p in sorted(sources)},
                'cases': args.cases, 'limits': {'vm_launches': 0 if args.prepare_only else len(args.cases), 'seconds_per_vm': 120},
                'excluded': ['pending compiler', 'forced per-grant HLT/AP overlap'],
                'artifacts': {} if args.prepare_only else {name: {'path': str(getattr(args, name)), 'sha256': windows.digest(getattr(args, name))}
                                                           for name in ('host', 'runtime', 'fexc')}}
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    prepared = []
    for case in args.cases:
        work = args.out / case; work.mkdir(); prepared.append((case, work, fixture(case, work)))
    if args.prepare_only: return 0
    results = []
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for case, work, config in prepared:
            results.append(run(args, case, work, config))
            if not results[-1]['pass']: break
    (args.out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    return 0 if len(results) == len(args.cases) and all(r['pass'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
