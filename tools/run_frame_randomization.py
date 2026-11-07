#!/usr/bin/env python3
"""Test per-VM frame randomization, including its deliberately retained selected-page hole.

Caller holds the VM lock. Selected-page successes are documented deviations, not
architectural passes. Neighbor pages must #GP without effects. The strict guard
runner and its architectural oracle remain separate and unchanged.
"""
import argparse
import hashlib
import json
import pathlib
import struct
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import run_frame_alias as strict

START, END = 0xffff400000000000, 0xffff408000000000
ALIASES = [('legacy', strict.FRAME), ('distant', 0xffff407abcde0000)]
MARKER = 0x123456789abcdef0


def valid_alias(va):
    return START <= va < END and va % 4096 == 0


def execute(a, text, work, alias=None):
    work.mkdir(parents=True, exist_ok=False)
    (work / 'program.txt').write_text(text)
    cmd = [str(a.host), str(a.runtime), 'run-program', str(a.fexc), str(work / 'program.txt'), str(work),
           '--watchdog-ms', '5000', '--code-shape']
    if alias is not None:
        cmd += ['--guest-state-alias', hex(alias)]
    if a.max_block:
        cmd += [str(a.max_block)]
    (work / 'command.json').write_text(json.dumps(cmd) + '\n')
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        (work / 'stdout').write_text(p.stdout)
        (work / 'stderr').write_text(p.stderr)
        return json.loads(p.stdout) if p.returncode == 0 else None, p.returncode
    except subprocess.TimeoutExpired as e:
        (work / 'stderr').write_text(f'timeout after30 seconds: {e.stderr!r}')
        return None, 'timeout'


def alias_errors(got, alias=None):
    cpus = got.get('cpus', [got])
    values = [strict.ref.num(c.get('guest_state_alias', 0)) for c in cpus]
    return ([] if values and len(set(values)) == 1 and all(valid_alias(v) for v in values)
            and (alias is None or values[0] == alias) else [f'wrong selected aliases: {list(map(hex, values))}'])


def selected_judge(before, got, end, alias, store):
    """The selected padding is accessible today. Require that deviation explicitly."""
    errors = alias_errors(got, alias)
    if got['stop'] != 'reached' or strict.ref.num(got['rip']) != end:
        errors.append('selected-page access did not complete (known deviation changed)')
    if got.get('frame_intact') is not (not store):
        errors.append('wrong frame sentinel: padding store must be observable')
    for key in strict.STATE:
        if key != 'gpr' and got.get(key) != before.get(key):
            errors.append(f'{key} changed')
    regs = list(map(strict.ref.num, before['gpr']))
    regs[0] = MARKER if store else 0
    if list(map(strict.ref.num, got['gpr'])) != regs:
        errors.append('selected-page load/store returned wrong value or changed other registers')
    return errors


def transition_program(n, cpl, alias=None):
    """Real frame fields survive links, CPUID, CR3 reset and both CPL switches.

    With an override, each CPU writes private padding, waits until all peers have
    written, then reads its own marker. Shared backing cannot pass this barrier.
    """
    data, raw, cpus, heads = strict.DATA, bytearray(0x4000), [], []
    raw[0x800:0x808] = (0x1122334455667788).to_bytes(8, 'little')
    raw[0x808:0x810] = (0x8877665544332211).to_bytes(8, 'little')
    raw[0x820:0x840] = bytes(range(32))
    for i in range(n):
        address, output = strict.CODE + i * 0x10000, data + 0x1000 + i * 0x100
        body = f'fninit\nfld1\nvmovdqu ymm1, [{data + 0x820:#x}]\n'
        if alias is not None:
            body += (f'mov rdi, {alias + 0xff0:#x}\nmov r13, [rdi]\nmov rbx, {MARKER + i:#x}\nmov [rdi], rbx\n'
                     f'mov qword ptr [{data + i * 8:#x}], 1\nwait_peers: xor eax, eax\n')
            body += ''.join(f'add rax, [{data + j * 8:#x}]\n' for j in range(n))
            body += f'cmp rax, {n}\njne wait_peers\n'
        body += ('mov ecx, 32\nloop_body: inc r12\ndec ecx\njnz loop_body\nxor eax, eax\ncpuid\n'
                 'sw0: nop\nmov rax, cr3\nmov cr3, rax\nsw3: nop\n')
        if alias is not None:
            body += 'mov r8, [rdi]\n'
        body += (f'mov r10, fs:[0]\nmov r11, gs:[0]\nfstp qword ptr [{output:#x}]\n'
                 f'vmovdqu [{output + 0x20:#x}], ymm1\nfnstcw word ptr [{output + 8:#x}]\nstmxcsr dword ptr [{output + 12:#x}]\n')
        code, labels = strict.ref.assemble(body, labels=True)
        heads.append(f'body {address:#x} {code.hex()}')
        regs = [0] * 16
        regs[4] = data + 0x3000 + i * 0x100
        cpus.append('\n'.join(['cpu', f'rip {address:#x}', f'cpl {cpl}', 'rflags 0x202', 'sse 1', 'xcr0 7', 'ne 1',
                               f'fsbase {data + 0x800:#x}', f'gsbase {data + 0x808:#x}',
                               f'switch {address + labels["sw0"]:#x} 0', f'switch {address + labels["sw3"]:#x} 3',
                               'gpr ' + ' '.join(hex(v) for v in regs)]))
    return '\n'.join(heads + [f'memory {data:#x} {raw.hex()}'] + cpus) + '\n'


def transition_judge(got, n, alias=None):
    errors = alias_errors(got, alias)
    cpus = got.get('cpus', [got])
    if len(cpus) != n:
        return errors + [f'CPU count {len(cpus)} != {n}']
    for i, cpu in enumerate(cpus):
        g = list(map(strict.ref.num, cpu['gpr']))
        if cpu['stop'] != 'reached' or cpu.get('frame_intact') is not (alias is None):
            errors.append(f'cpu{i} completion/frame sentinel mismatch')
        if (g[10], g[11], g[12]) != (0x1122334455667788, 0x8877665544332211, 32):
            errors.append(f'cpu{i} FS/GS/loop result mismatch')
        if alias is not None and (g[8], g[13]) != (MARKER + i, 0):
            errors.append(f'cpu{i} private frame marker mismatch')
        output = strict.DATA + 0x1000 + i * 0x100
        for at, want in [(output, struct.pack('<d', 1.0)), (output + 8, b'\x7f\x03'),
                         (output + 12, bytes.fromhex('801f0000')), (output + 0x20, bytes(range(32)))]:
            if strict.memory(cpu, at, len(want)) != want:
                errors.append(f'cpu{i} x87/vector state mismatch at {at:#x}')
        if cpu['stats']['systems'] < 3:
            errors.append(f'cpu{i} expected CPUID/CR3/CPL transitions did not execute')
    return errors


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ('host', 'runtime', 'fexc', 'out'):
        ap.add_argument('--' + name, type=pathlib.Path, required=name in ('fexc', 'out'),
                        default=ROOT / ('target/release/natemu-host' if name == 'host' else 'target/aarch64-unknown-none/release/natemu-runtime') if name in ('host', 'runtime') else None)
    ap.add_argument('--max-block', type=int)
    ap.add_argument('--only', default='')
    ap.add_argument('--samples', type=int, default=8)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)
    (a.out / 'inputs.json').write_text(json.dumps({str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [a.host, a.runtime, a.fexc, pathlib.Path(__file__), pathlib.Path(strict.__file__)]}, indent=2) + '\n')
    rows, sampled = [], []

    def record(name, errors, **extra):
        rows.append(dict(name=name, errors=errors, **extra))
        (a.out / 'results.json').write_text(json.dumps(rows, indent=2) + '\n')
        print(name, 'FAIL' if errors else 'PASS', errors, flush=True)

    for tag, alias in ALIASES:
        for cpl in (0, 3):
            for kind, offset, store in [('selected-load', 0, False), ('selected-store', 0, True),
                                        ('before-load', -4096, False), ('before-store', -4096, True),
                                        ('after-load', 4096, False), ('after-store', 4096, True)]:
                name = f'{tag}-{kind}-cpl{cpl}'
                if a.only not in name:
                    continue
                c = dict(name=name, regs={7: alias + offset + 0xff0, 3: MARKER}, fault=13,
                         insn='mov [rdi], rbx\nmov rax, [rdi]' if store else 'mov rax, [rdi]')
                text, at, end = strict.description(c, cpl)
                before, rc0 = execute(a, strict.description(c, cpl, True)[0], a.out / name / 'prefix', alias)
                got, rc = execute(a, text, a.out / name / 'probe', alias)
                extra = dict(selected_alias=hex(alias), classification='known selected-page deviation' if offset == 0 else 'architectural GP')
                if not before or before['stop'] != 'reached' or not got:
                    errors = [f'prefix/probe failed: {rc0}/{rc}']
                elif offset == 0:
                    errors = alias_errors(before, alias) + selected_judge(before, got, end, alias, store)
                    extra['strict_guard_rejection'] = strict.judge(c, before, got, at, end)
                    if not extra['strict_guard_rejection']:
                        errors.append('strict architectural oracle unexpectedly accepted the selected-page hole')
                else:
                    errors = alias_errors(before, alias) + alias_errors(got, alias) + strict.judge(c, before, got, at, end)
                record(name, errors, **extra)
            for n in (1, 4):
                name = f'{tag}-private-transitions-cpl{cpl}-n{n}'
                if a.only not in name:
                    continue
                got, rc = execute(a, transition_program(n, cpl, alias), a.out / name, alias)
                record(name, transition_judge(got, n, alias) if got else [f'driver failed: {rc}'],
                       classification='known selected-page deviation plus private-frame and transition checks')
    for i in range(a.samples):
        n = 1 if i % 2 == 0 else 4
        name = f'random-transitions-{i}-n{n}'
        if a.only not in name:
            continue
        got, rc = execute(a, transition_program(n, i % 2 * 3), a.out / name)
        errors = transition_judge(got, n) if got else [f'driver failed: {rc}']
        values = [strict.ref.num(c['guest_state_alias']) for c in got.get('cpus', [got])] if got else []
        if values:
            sampled.append(values[0])
        record(name, errors, selected_aliases=list(map(hex, values)))
    if sampled:
        record('random-selection-sanity', [] if len(sampled) < 2 or len(set(sampled)) > 1 else ['all VMs reused one alias'],
               samples=list(map(hex, sampled)), unique=len(set(sampled)),
               limitation='finite sampling only checks startup variation; entropy strength is established by the OS API and mapping tests')
    print(f'{len(rows)} checks, {sum(bool(r["errors"]) for r in rows)} failed', flush=True)
    return int(not rows or any(r['errors'] for r in rows))


if __name__ == '__main__':
    sys.exit(main())
