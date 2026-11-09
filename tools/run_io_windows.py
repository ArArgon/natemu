#!/usr/bin/env python3
"""Live copy-window development checks, using the production run-program coordinator.

Run from the retained source used to build HOST/RUNTIME. No builds or automatic retries.
Three serial VMs (N=1,2,4), at most 120 seconds each. Full inputs, logs, artifact/source
hashes and verdicts are retained. These are exploratory checks, not performance evidence.
HLT/AP, high RAM, device reset/IRQ and compiler-timeout cases are outside this first slice.
"""
import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

from io_common import digest, stop_group, ROOT, VM_LOCK
sys.path.insert(0, str(ROOT / 'tools' / 'reference'))
import ref  # noqa: E402

BASE, CODE, FUNCTION, STACK = 0x200000, 0x100000, 0x110000, 0x300000


def cache_preservation_errors(cpus, *, ordinary_writes=False):
    # This tiny fixture has room for all its code. Its host writes include both
    # ordinary data and a code patch; neither requires discarding unrelated code.
    errors = [f'CPU {v} discarded unrelated cached code ({cpu["stats"]["k1"]["flushes"]} full flushes)'
              for v, cpu in enumerate(cpus) if cpu['stats']['k1']['flushes'] > 1]
    errors += [f'CPU {v} invalidated mappings for a data/code write'
               for v, cpu in enumerate(cpus) if ordinary_writes and cpu['stats']['k1']['global_flushes'] != 0]
    return errors


def fixture(n, work):
    memory = bytearray(0x308)
    memory[0x3c:0x44] = b'\x55' * 8
    lines = ['identity', f'body {FUNCTION:#x} b801000000c3',
             f'memory {BASE:#x} {memory.hex()}']
    for v in range(n):
        assembly = f'''mov rdi, {BASE:#x}
again:
    lock not qword ptr [rdi + 0x3c]
    inc qword ptr [rdi + {0x100 + 8 * v:#x}]
    mov rax, {FUNCTION:#x}
    call rax
    mov qword ptr [rdi + {0x200 + 8 * v:#x}], rax
    cmp byte ptr [rdi], 0
    je again
    nop
'''
        (work / f'cpu-{v}.asm').write_text('.intel_syntax noprefix\n' + assembly)
        code = ref.assemble(assembly)
        lines += [f'body {CODE + v * 0x1000:#x} {code.hex()}', f'region {STACK + v * 0x1000:#x} 0x1000']
    for v in range(n):
        regs = [0] * 16
        regs[4] = STACK + v * 0x1000 + 0xff0
        lines += ['cpu', f'rip {CODE + v * 0x1000:#x}', 'gpr ' + ' '.join(hex(r) for r in regs),
                  'cpl 0', 'rflags 0x2', 'if 0']
    (work / 'program.txt').write_text('\n'.join(lines) + '\n')


def run_case(args, n):
    work = args.out / f'n{n}'
    work.mkdir()
    fixture(n, work)
    command = [str(args.host), str(args.runtime), 'run-program', str(args.fexc),
               str(work / 'program.txt'), str(work), '--watchdog-ms', '60000',
               '--ram', '0x4000000', '--code-shape', '--io-windows', hex(BASE),
               '--io-window-code', hex(FUNCTION)]
    (work / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    started = time.monotonic()
    timed_out = False
    with (work / 'stdout.log').open('wb') as stdout, (work / 'stderr.log').open('wb') as stderr:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_group(process)
        except BaseException:
            stop_group(process)
            raise
    result = {'n': n, 'preserve_code': args.preserve_code, 'command': command, 'returncode': process.returncode,
              'seconds': time.monotonic() - started, 'timeout': timed_out, 'errors': []}
    errors = result['errors']
    if timed_out or process.returncode != 0: errors.append('VM did not exit successfully')
    stderr = (work / 'stderr.log').read_text(errors='replace')
    expected = f'teardown: vcpus {n} joined {n} destroyed {n} vm_destroy 0 children none'
    if expected not in stderr.splitlines(): errors.append('missing clean teardown')
    try:
        report = json.loads((work / 'stdout.log').read_text())
        cpus, io = report['cpus'], report['io_windows']
        if args.preserve_code: errors.extend(cache_preservation_errors(cpus, ordinary_writes=True))
        if len(cpus) != n: errors.append('wrong participant count')
        for v, cpu in enumerate(cpus):
            if cpu['stop'] != 'reached': errors.append(f'CPU {v} did not reach fixture end')
            if cpu['stats']['splits'] <= 0: errors.append(f'CPU {v} did not exercise split accesses')
        if io['snapshots'] != 64 or io['windows'] < 68: errors.append('insufficient live windows')
        if not io['ordered_overlap'] or io['code_observed'] != 2: errors.append('overlap or code patch failed')
        before, after = io['progress_before'], io['progress_after']
        if len(before) != n or len(after) != n or any(b <= a for a, b in zip(before, after)):
            errors.append('missing per-CPU progress')
        result['io_windows'] = io
        result['splits'] = [cpu['stats']['splits'] for cpu in cpus]
    except (ValueError, KeyError, TypeError) as error:
        errors.append(f'invalid driver report: {error}')
    result['pass'] = not errors
    (work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(f"N={n}: {'PASS' if result['pass'] else 'FAIL'} ({result['seconds']:.2f}s) {errors}", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('host', 'runtime', 'fexc', 'out'):
        parser.add_argument('--' + option, type=pathlib.Path, required=True)
    parser.add_argument('--lock', type=pathlib.Path, default=VM_LOCK)
    parser.add_argument('--cpus', type=int, nargs='+', choices=(1, 2, 4), default=[1, 2, 4])
    parser.add_argument('--preserve-code', action='store_true', help='also require unrelated compiled code to survive I/O writes')
    args = parser.parse_args()
    if len(set(args.cpus)) != len(args.cpus): parser.error('no duplicate VM cases')
    for option in ('host', 'runtime', 'fexc'):
        path = getattr(args, option).resolve(strict=True)
        setattr(args, option, path)
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    sources = [p for p in (ROOT / 'crates').rglob('*') if p.is_file()]
    sources += [ROOT / p for p in ('Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml',
                                 'tools/run_io_windows.py', 'tools/reference/ref.py', 'tools/reference/flags.py')]
    manifest = {'purpose': 'development/exploratory; not decision evidence', 'source_root': str(ROOT),
                'source_sha256': {str(p.relative_to(ROOT)): digest(p) for p in sorted(sources) if p.is_file()},
                'artifacts': {key: {'path': str(getattr(args, key)), 'sha256': digest(getattr(args, key))}
                              for key in ('host', 'runtime', 'fexc')},
                'limits': {'vm_launches': len(args.cpus), 'seconds_per_vm': 120}, 'preserve_code': args.preserve_code,
                'excluded': ['HLT/AP', 'high RAM', 'reset/IRQ', 'compiler timeout']}
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    results = []
    with args.lock.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for n in args.cpus:
            result = run_case(args, n)
            results.append(result)
            # Retain the first failure; do not spend further launches or hide it with retries.
            if not result['pass']: break
    (args.out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    return 0 if len(results) == len(args.cpus) and all(r['pass'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
