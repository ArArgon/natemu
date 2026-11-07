#!/usr/bin/env python3
"""Mixed resident vector loads and split atomics on adjacent pages, three vCPUs.

Run under the shared VM lock. Writers flip all bits of unaligned qwords. Each
aligned eight-byte piece read by the crossing vector may see either generation,
but must never see a partially updated subset of the locked qword's bytes.
"""
import argparse
import json
import pathlib
import sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools/reference')]
import ref
import run_slice3_vm as vm

DATA, FLAG, LOOPS = 0x200000, 0x203000, 20000


def program():
    bodies, cpus = [], []
    for cpu in range(3):
        start = 0x110000 + 0x1000 * cpu
        if cpu < 2:
            operand = DATA + (0xfee if cpu == 0 else 0x100c)
            text = f'''lock or dword ptr [{FLAG:#x}], {1 << cpu}
wait: cmp dword ptr [{FLAG:#x}], 7
jne wait
mov ecx, {LOOPS * 2}
loop: lock xor qword ptr [{operand:#x}], -1
inc r13
loop loop
nop
'''
            code = ref.assemble(text)
            setup = ''
        else:
            text = f'''mov rax, cr0
bts rax, 18
mov cr0, rax
switch:
nop
lock or dword ptr [{FLAG:#x}], 4
wait: cmp dword ptr [{FLAG:#x}], 7
jne wait
mov ecx, {LOOPS}
loop:
vmovdqu ymm0, [{DATA + 0xfe8:#x}]
vmovq rax, xmm0
shr rax, 48
cmp eax, 0
je low_ok
cmp eax, 65535
jne bad
low_ok:
vpextrq rax, xmm0, 1
shl rax, 16
cmp rax, 0
je high_ok
cmp rax, -65536
jne bad
high_ok:
inc r13
loop loop
jmp done
bad:
mov r12d, 1
done:
nop
'''
            code, labels = ref.assemble(text, labels=True)
            setup = f'switch {start + labels["switch"]:#x} 3\nrflags 0x40002\nsse 1\nxcr0 7\n'
        bodies.append(f'body {start:#x} {code.hex()}')
        cpus.append(f'cpu\nrip {start:#x}\ncpl 0\nif 0\n{setup}')
    return '\n'.join([*bodies, f'memory {DATA:#x} ' + bytes(0x4000).hex(), *cpus]) + '\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--host', type=pathlib.Path, default=ROOT / 'target/release/natemu-host')
    ap.add_argument('--runtime', type=pathlib.Path, default=ROOT / 'target/aarch64-unknown-none/release/natemu-runtime')
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    a = ap.parse_args()
    rows = []
    for block in (None, 1):
        work = a.out / f'block-{block}'
        rc, output, error = vm.driver(a.host, a.runtime, a.fexc, program(), work, [block] if block else [], timeout=30)
        (work / 'result.json').write_text(json.dumps(output, indent=2) + '\n')
        (work / 'stderr').write_text(error)
        errors = []
        if not output:
            errors.append(f'no output: {rc} {error[-300:]}')
        else:
            cpus = output['cpus']
            for i, c in enumerate(cpus):
                if c['stop'] != 'reached': errors.append(f'cpu{i} stop {c["stop"]}')
                if int(c['gpr'][12], 0): errors.append(f'cpu{i} saw partial locked value')
                want = LOOPS * 2 if i < 2 else LOOPS
                if int(c['gpr'][13], 0) != want: errors.append(f'cpu{i} incomplete iterations')
                if c['stats']['splits'] != want: errors.append(f'cpu{i} did not use resident completion for every access')
            image = bytes.fromhex(next(m['data'] for m in cpus[0]['memory'] if int(m['addr'], 0) == DATA))
            for offset in (0xfee, 0x100c):
                if image[offset:offset + 8] != bytes(8): errors.append(f'final counter {offset:x} not zero')
            rows.append({'block': block, 'errors': errors, 'completions': [c['stats']['splits'] for c in cpus]})
        if not output: rows.append({'block': block, 'errors': errors})
        print(rows[-1], flush=True)
    (a.out / 'results.json').write_text(json.dumps(rows, indent=2) + '\n')
    return int(any(r['errors'] for r in rows))


if __name__ == '__main__':
    sys.exit(main())
