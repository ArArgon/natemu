#!/usr/bin/env python3
"""Bounded CPU/RAM regressions: bit-test footprint, INSERTPS loads and memory carry rotates.

Run under the shared VM lock; upstream test sources are not modified.
"""
import argparse
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools/reference')]
import ref

CODE, DATA = 0x100000, 0x200000
INITIAL = 0x816243248566a788


def rotate(value, bits, count, right, flags):
    count &= 63 if bits == 64 else 31
    masked = count
    count %= bits + 1
    mask = (1 << bits) - 1
    cf = flags & 1
    old = value & mask
    value = old
    for _ in range(count):
        if right:
            value, cf = (value >> 1) | (cf << (bits - 1)), value & 1
        else:
            value, cf = ((value << 1) | cf) & mask, value >> (bits - 1)
    if count:
        flags = (flags & ~1) | cf
        if count == 1:
            of = ((value >> (bits - 1)) ^ (value >> (bits - 2))) & 1 if right else ((value >> (bits - 1)) ^ cf) & 1
            flags = (flags & ~(1 << 11)) | (of << 11)
    return (INITIAL & ~mask) | value, flags, 0xcd5 if masked in (0, 1) else 0x4d5


def cases():
    for right in (False, True):
        result = INITIAL >> 1 if right else (INITIAL << 1) & ((1 << 64) - 1)
        cf = INITIAL & 1 if right else INITIAL >> 63
        flags = (0xad7 & ~0x8c5) | cf | ((result == 0) << 6) | ((result >> 63) << 7) | ((bin(result & 255).count('1') % 2 == 0) << 2) | (((INITIAL ^ result) >> 63) << 11)
        yield dict(name=f'66-rexw-{"shrd" if right else "shld"}', insn='.byte 0x66\n' + ('shrd' if right else 'shld') + ' rbx, rdx, cl',
                   addr=DATA, count=1, flags=0xad7, regs={2:0xa53c, 3:INITIAL}, value=INITIAL,
                   wantflags=flags, flagmask=0x8c5, wantregs={3:result})
    for right in (False, True):
        op = 'shrd' if right else 'shld'
        for count in (*range(17), 32, 33, 48):
            for memory in (False, True):
                n, flags, dest, source = count & 31, 0xad7, INITIAL & 0xffff, 0xa53c
                result = dest
                if n:
                    result = (((source << 16) | dest) >> n if right else ((dest << 16) | source) >> (16-n)) & 0xffff
                    cf = ((dest >> (n-1)) if right else (dest >> (16-n))) & 1
                    flags = (flags & ~0xc5) | cf | ((result == 0) << 6) | ((result >> 15) << 7) | ((bin(result & 255).count('1') % 2 == 0) << 2)
                    if n == 1: flags = (flags & ~0x800) | (((dest ^ result) >> 15) << 11)
                value = (INITIAL & ~0xffff) | result
                yield dict(name=f'{op}16-cl{count}-{"memory" if memory else "register"}', insn=f'{op} ' + ('word ptr [rax]' if memory else 'bx') + ', dx, cl',
                           addr=DATA, count=count, flags=0xad7, regs={2:source, 3:INITIAL}, value=value if memory else INITIAL,
                           wantflags=flags, flagmask=0xcd5 if n == 0 else 0xcc5 if n == 1 else 0xc5,
                           wantregs={} if memory else {3:value})
        for protection in ('unmap', 'readonly'):
            yield dict(name=f'{op}16-cl0-{protection}', insn=f'{op} word ptr [rax], dx, cl', addr=DATA,
                       setup=[f'{protection} {DATA:#x}'], fault=(14, DATA, 2 | int(protection == 'readonly')))
        yield dict(name=f'{op}16-cl0-ac', insn=f'{op} word ptr [rax], dx, cl', addr=DATA+1,
                   flags=0x40202, ac=True, cpls=(3,), fault=(17, 0, 0))
        yield dict(name=f'{op}16-cl0-cross', insn=f'{op} word ptr [rax], dx, cl', addr=DATA+4095,
                   setup=[f'unmap {DATA+4096:#x}'], fault=(14, DATA+4096, 2))
        for count in (17, 31):
            yield dict(name=f'{op}16-cl{count}-undefined', insn=f'{op} bx, dx, cl', addr=DATA, count=count, fault=(6, 0, 0))
    for insn in ('bt qword ptr [rax], 0', 'rcl qword ptr [rax], cl', 'rcr qword ptr [rax], cl'):
        yield dict(name=insn.split()[0]+'-watch-last-byte', insn=insn, addr=DATA, watch=True, fault=(1, 0, 0))
    yield dict(name='pushf16-value', insn='pushfw', addr=DATA, regs={4:DATA+2}, value=INITIAL & ~0xffff | 0x202,
               wantflags=0x202, flagmask=0xffff, wantregs={4:DATA})
    yield dict(name='leave16-value', insn='data16 leave', addr=DATA, regs={5:DATA}, value=INITIAL,
               wantflags=0x202, flagmask=0xffff, wantregs={4:DATA+2, 5:(DATA & ~0xffff) | (INITIAL & 0xffff)})
    for form in ('pushf', 'leave'):
        yield dict(name=f'{form}16-fault', insn='pushfw' if form=='pushf' else 'data16 leave', addr=DATA,
                   regs={4:DATA+2} if form=='pushf' else {5:DATA}, setup=[f'unmap {DATA:#x}'], fault=(14, DATA, 2 if form=='pushf' else 0))
    for bits in (16, 32, 64):
        width, word = bits // 8, {16: 'word', 32: 'dword', 64: 'qword'}[bits]
        for op in ('bt', 'bts', 'btr', 'btc'):
            for lock in ('', 'lock ') if op != 'bt' else ('',):
                for offset in (-bits - 1, -1, 0, bits - 1, bits, bits + 1):
                    bit = offset % bits
                    value = INITIAL
                    if op == 'bts': value |= 1 << bit
                    if op == 'btr': value &= ~(1 << bit)
                    if op == 'btc': value ^= 1 << bit
                    yield dict(name=f'{lock}{op}{bits}-index{offset}', insn=f'{lock}{op} {word} ptr [rax], ' + {16:'cx',32:'ecx',64:'rcx'}[bits],
                               addr=DATA - (offset // bits) * width, count=offset & ((1 << 64) - 1),
                               value=value, wantflags=(INITIAL >> bit) & 1, flagmask=1)
                yield dict(name=f'{lock}{op}{bits}-cross', insn=f'{lock}{op} {word} ptr [rax], 0', addr=DATA + 4096 - width + 1,
                           setup=[f'unmap {DATA + 4096:#x}'], fault=(14, DATA + 4096, 2 if op != 'bt' else 0))
                yield dict(name=f'{lock}{op}{bits}-canonical', insn=f'{lock}{op} {word} ptr [rax], 0',
                           addr=0x800000000000 - width + 1, fault=(13, 0, 0))
                yield dict(name=f'{lock}{op}{bits}-ac', insn=f'{lock}{op} {word} ptr [rax], 0', addr=DATA+1,
                           flags=0x40202, ac=True, cpls=(3,), fault=(17, 0, 0))
    for vex in (False, True):
        for imm in (0x01, 0x12, 0x24, 0x38, 0x0f):
            insn = f'vinsertps xmm0, xmm1, dword ptr [rax], {imm}' if vex else f'insertps xmm0, dword ptr [rax], {imm}'
            yield dict(name=f'{"v" if vex else ""}insertps-{imm:x}-unmapped', insn=insn, addr=DATA,
                       setup=[f'unmap {DATA:#x}'], fault=(14, DATA, 0))
    for bits in (8, 16, 32, 64):
        word = {8: 'byte', 16: 'word', 32: 'dword', 64: 'qword'}[bits]
        for right in (False, True):
            op = 'rcr' if right else 'rcl'
            if bits > 8:
                for offset in (1, 4095):
                    value, flags, mask = rotate(INITIAL, bits, 1, right, 0xad7)
                    yield dict(name=f'{op}{bits}-unaligned{offset}', insn=f'{op} {word} ptr [rax], cl', addr=DATA+offset,
                               data=DATA+offset, count=1, flags=0xad7, value=value, wantflags=flags, flagmask=mask)
            for count in (0, 1, 8, 9, 16, 17, 31, 32, 63, 64, 255):
                for cf in (0, 1):
                    flags = 0xad6 | cf
                    value, wantflags, mask = rotate(INITIAL, bits, count, right, flags)
                    yield dict(name=f'{op}{bits}-cl{count}-cf{cf}', insn=f'{op} {word} ptr [rax], cl', addr=DATA,
                               count=count, flags=flags, value=value, wantflags=wantflags, flagmask=mask)
            for form in ('cl', '0', '64' if bits == 64 else '32'):
                for protection in ('unmap', 'readonly'):
                    yield dict(name=f'{op}{bits}-{form}-{protection}', insn=f'{op} {word} ptr [rax], {form}', addr=DATA,
                               setup=[f'{protection} {DATA:#x}'], fault=(14, DATA, 2 | int(protection == 'readonly')))
            if bits > 8:
                yield dict(name=f'{op}{bits}-cl0-cross-protection', insn=f'{op} {word} ptr [rax], cl', addr=DATA+4095,
                           setup=[f'readonly {DATA:#x}', f'unmap {DATA+4096:#x}'], fault=(14, DATA+4095, 3))
                yield dict(name=f'{op}{bits}-cl0-ac', insn=f'{op} {word} ptr [rax], cl', addr=DATA+1,
                           flags=0x40202, ac=True, cpls=(3,), fault=(17, 0, 0))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--host', type=pathlib.Path, default=ROOT / 'target/release/natemu-host')
    ap.add_argument('--runtime', type=pathlib.Path, default=ROOT / 'target/aarch64-unknown-none/release/natemu-runtime')
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--only', default='')
    a = ap.parse_args()
    rows = []
    for c in cases():
        if a.only and a.only not in c['name']: continue
        prefix = ref.assemble('mov rdx, cr0; or edx, 0x40000; mov cr0, rdx; mov edx, 0; push 0x40202; popfq; nop') if c.get('ac') else b''
        if c.get('watch'): prefix = ref.assemble(f'mov edx, {DATA+7}; mov dr0, rdx; mov edx, 0x30001; mov dr7, rdx; mov edx, 0; nop')
        insn = ref.assemble(c['insn'])
        code = prefix + insn + b'\x90'
        fault_rip = CODE + len(prefix)
        for cpl in c.get('cpls', (0, 3)):
            for block in (None, 1):
                name = f'{c["name"]}-cpl{cpl}-block{block}'
                d = a.out / name; d.mkdir(parents=True, exist_ok=True)
                regs = [0] * 16; regs[0] = c['addr']; regs[1] = c.get('count', 0); regs[4] = 0x3f0000
                for r, value in c.get('regs', {}).items(): regs[r] = value
                flags = c.get('flags', 0x202)
                data = c.get('data', DATA)
                text = '\n'.join([f'rip {CODE:#x}', f'cpl {0 if prefix else cpl}', f'rflags {flags:#x}', 'sse 1', 'xcr0 7',
                    'gpr ' + ' '.join(map(hex, regs)), f'code {code.hex()}', f'region {DATA:#x} 0x2000', 'region 0x3e0000 0x20000',
                    f'memory {data:#x} {INITIAL.to_bytes(8, "little").hex()}', 'xmm 0 0x1234 0x5678', 'xmm 1 0xabcd 0xef01',
                    *([f'switch {fault_rip-1:#x} {cpl}'] if prefix else []), *c.get('setup', [])]) + '\n'
                (d / 'program.txt').write_text(text)
                cmd = [str(a.host), str(a.runtime), 'run-program', str(a.fexc), str(d / 'program.txt'), str(d), '--watchdog-ms', '5000']
                if block: cmd.append(str(block))
                run = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                (d / 'stdout').write_text(run.stdout); (d / 'stderr').write_text(run.stderr)
                got = json.loads(run.stdout) if run.returncode == 0 else None
                errors = []
                if not got: errors.append('driver failed')
                elif 'fault' in c:
                    v, addr, err = c['fault']; err |= 4 if cpl == 3 and v == 14 else 0
                    want = [v, addr, err, fault_rip + (len(insn) if c.get('watch') else 0)]
                    if got['stop'] != 'guest_fault' or list(map(ref.num, got.get('fault', []))) != want:
                        errors.append(f'fault {got.get("fault")}, expected {want}')
                    if ref.num(got['rflags']) != flags: errors.append('flags changed at fault')
                    if list(map(ref.num, got['gpr'])) != regs: errors.append('registers changed at fault')
                    if got.get('xmm') and list(map(ref.num, got['xmm'][:2])) != [0x1234, 0x5678]: errors.append('XMM0 changed at fault')
                    raw = next(m['data'] for m in got['memory'] if ref.num(m['addr']) == data)
                    if int.from_bytes(bytes.fromhex(raw)[:8], 'little') != INITIAL: errors.append('memory changed at fault')
                else:
                    if got['stop'] != 'reached': errors.append(f'stop {got["stop"]}')
                    raw = next(m['data'] for m in got['memory'] if ref.num(m['addr']) == data)
                    if int.from_bytes(bytes.fromhex(raw)[:8], 'little') != c['value']: errors.append('memory result')
                    if (ref.num(got['rflags']) ^ c['wantflags']) & c['flagmask']: errors.append('defined flags')
                    for r, value in c.get('wantregs', {}).items():
                        if ref.num(got['gpr'][r]) != value: errors.append(f'register {r}')
                rows.append(dict(name=name, errors=errors))
                if errors: print(name, errors, flush=True)
    (a.out / 'results.json').write_text(json.dumps(rows, indent=2) + '\n')
    bad = sum(bool(r['errors']) for r in rows)
    print(f'{len(rows)} executions, {bad} failed', flush=True)
    return bool(bad)


if __name__ == '__main__': sys.exit(main())
