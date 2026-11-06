#!/usr/bin/env python3
"""Seeded generator for the REX-chain corpus (R2 review, census): 64-bit code with several REX prefixes before one opcode.

    python3 tools/reference/gen_rex.py -o OUT.json [--seed N]

Only a REX immediately before the opcode counts; with several in a row, the last one (SDM Vol.2 2.2.1: a REX elsewhere is
ignored). OpenSSL's CRYPTOGAMS strings in `.text` decode as such chains, the last census sites (`4f 47 41 4d 53` push r11,
`47 46 28 32` subb %r14b,(%rdx), `48 41 31 20` xorl %esp,(%r8)). Programs: `rex-sites` runs those three byte for byte;
`rex-*` are slice-1 random programs (gen_slice1.Prog) in which every instruction encoded with a REX gets 1-3 random REX
bytes inserted before it, so the inserted ones (W, R, X, B set at random) must be ignored. `seg-branches-*` (R2 review,
owner rule: match the processor where the SDM says reserved): near direct branches (Jcc rel8/rel32 backward loops, Jcc rel8,
JMP rel8/rel32, CALL rel32, JRCXZ to the next instruction, LOOP) with 1-2 random segment prefixes (26/2E/36/3E/64/65;
FS/GS bases nonzero), which processors and QEMU ignore. Expected results come from ref.py under QEMU 7.2, whose decoder also keeps the last REX. A REX
followed by a legacy prefix (x86 ignores that REX) is covered by unit tests only (`system::tests::rex_chains`, the
admission decoder tests): pinned QEMU keeps such a REX.
"""
import argparse, json, pathlib, random, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gen_slice1 as g
import ref
from gen_slice1 import DATA

LEGACY = {0xF0, 0xF2, 0xF3, 0x66, 0x67, 0x2E, 0x36, 0x3E, 0x26, 0x64, 0x65}


def chain(p, i, extra):
    """Line `i` as bytes with `extra` REX bytes before its own REX (unchanged when it has none, or is RIP-relative)."""
    if '{' in p.lines[i]: return False
    b = ref.assemble(p.lines[i])
    k = 0
    while b[k] in LEGACY: k += 1
    if not 0x40 <= b[k] <= 0x4F: return False
    b = b[:k] + bytes(extra) + b[k:]
    if len(b) > 15: return False
    p.lines[i] = '.byte ' + ', '.join(f'{x:#04x}' for x in b)
    return True


def sites(rng):
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r); p = g.Prog('rex-sites', r)
    p.emit(f'mov rdx, {DATA + 0x100}'); p.emit(f'mov r8, {DATA + 0x208}')
    for text, raw in [('push r11', '4f47414d53'), ('sub byte ptr [rdx], r14b', '47462832'), ('xor dword ptr [r8], esp', '48413120'),
                      ('pop rax', None), ('push r11', '4f47414d53'), ('sub byte ptr [rdx], r14b', '47462832'), ('pop rcx', None)]:
        p.emit(text)
        if raw: p.lines[-1] = '.byte ' + ', '.join(f'{x:#04x}' for x in bytes.fromhex(raw))
    return g.finish(p, gpr, rf, r) | {'unannotated': True}


def random_program(name, rng):
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r); p = g.Prog(name, r)
    for _ in range(24): p.one()
    while p.depth: p.depth -= 1; p.emit(f'pop {p.reg(64, writable=True)}')
    n = sum(chain(p, i, [r.randrange(0x40, 0x50) for _ in range(r.randrange(1, 4))]) for i in range(len(p.lines)))
    assert n, name
    p.emit('add rax, rcx')    # every flag defined at the end
    return g.finish(p, gpr, rf, r) | {'unannotated': True}


CCS = ['e', 'ne', 'b', 'ae', 'l', 'ge', 'le', 'g', 's', 'ns', 'be', 'a']
JCC = 'o no b ae e ne be a s ns p np l ge le g'.split()
SEGS = [0x26, 0x2E, 0x36, 0x3E, 0x64, 0x65]


def branches(name, rng, prefixes=lambda r: r.sample(SEGS, r.randrange(1, 3))):
    """QEMU translates only what runs, so every byte must run: backward loops (taken, then not) and forward branches to the
    next instruction (rel 0: taken or not, the same path; CALL's pushed address checks the target). `prefixes(r)`: the
    prefix bytes before each branch (gen_br66.py: with 66)."""
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r); p = g.Prog(name, r)
    def raw(line):    # no flag effect
        p.lines.append(line); p.insns.append({'op': 'mov', 'width': 64})
    for k in range(10):
        pre = '.byte ' + ', '.join(f'{x:#04x}' for x in prefixes(r))
        form = r.choice(['jcc8', 'jcc32', 'jcc0', 'jmp8', 'jmp32', 'call', 'loop', 'jrcxz'])
        if form in ('jcc8', 'jcc32', 'loop'):
            p.emit(f'mov {"ecx" if form == "loop" else "r9d"}, {r.randrange(2, 4)}')
            p.lines.append(f't{k}:')
            p.emit(f'add rax, {r.randrange(1, 100)}')
            if form != 'loop': p.emit('dec r9d')
            raw(pre)
            raw({'jcc8': f'jne t{k}', 'jcc32': f'.byte 0x0f, 0x85\n.long t{k} - . - 4', 'loop': f'loop t{k}'}[form])
            continue
        p.emit(f'cmp {p.reg(64)}, {p.reg(64)}')
        if form == 'jrcxz': p.emit(f'mov ecx, {r.choice([0, 1])}')
        raw(pre)
        raw({'jcc0': f'.byte {0x70 + JCC.index(r.choice(CCS)):#04x}, 0', 'jmp8': '.byte 0xeb, 0', 'jmp32': '.byte 0xe9\n.long 0',
             'call': '.byte 0xe8\n.long 0', 'jrcxz': '.byte 0xe3, 0'}[form])
        if form == 'call': p.emit('pop rdx')
    p.emit('add rax, rcx')
    return g.finish(p, gpr, rf, r) | {'unannotated': True, 'fsbase': hex(0x1000_0000), 'gsbase': hex(0x2000_0000)}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261102)
    a = ap.parse_args(); rng = random.Random(a.seed)
    progs = [sites(rng)] + [random_program(f'rex-{i:03d}', rng) for i in range(24)] + [branches(f'seg-branches-{i:03d}', rng) for i in range(12)]
    clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    a.output.write_text(json.dumps({'generator': {'script': 'tools/reference/gen_rex.py', 'seed': a.seed, 'assembler': clang,
        'layout': {'code': hex(g.CODE), 'data': hex(DATA)}}, 'programs': progs}, indent=1) + '\n')
    print(f'{len(progs)} programs -> {a.output}')

if __name__ == '__main__': main()
