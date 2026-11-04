#!/usr/bin/env python3
"""Seeded generator for the ADX corpus: ADCX and ADOX (CPUID.7.0:EBX.ADX[19], advertised since November 3; boot-features plan,
Built (ADX)).

    python3 tools/reference/gen_adx.py -o OUT.json [--seed N]
    python3 tools/reference/gen_adx.py -o OUT.json --check EXPECTED.json   # the `adx-model-*` programs through the SDM model

Each program starts from random general registers (often boundary values) and random arithmetic flags. Families:
  - `adx-model-*` (16): only ADCX/ADOX at 32 and 64 bits, register and memory sources, and STC/CLC/CMC: final registers, CF
    and OF replayed through the SDM's Operation (`--check`; every other flag must keep its initial value);
  - `adx-chain-*` (16): MULX + ADCX/ADOX interleaved as in the multi-precision multiply loops of OpenSSL/GMP (two carry chains
    at once), after an ADD that defines every flag, with PUSHFQ between steps: the other five flags must be untouched;
  - `adx-mix-*` (24): ADCX/ADOX between random general instructions (gen_slice1's), so the flags before each come from any
    producer (shifts, BT, MUL, logic, INC/DEC, SAHF, CMC...), register and memory operands near and across the page boundary,
    destination = source, 32-bit forms with garbage upper halves (zero-extended), raw encodings with REX.W, 0x67 and ignored
    DS/ES/CS/SS prefixes, and ADOX with a 66 before or after its F3 (66 ignored on the AMD machine: owner rule, November 4).
The reference is the AMD machine (`ref_native.py --require-flag adx`); pinned QEMU 7.2's TCG implements ADX too (`ref.py`).
"""
import argparse, json, pathlib, random, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ref, flags, gen_slice1 as g
from gen_slice1 import DATA, NAMES, PTRS

CPU = 'qemu64,+adx,+bmi2'
SCR = [0, 1, 2, 9, 10, 11, 14, 15]   # writable scratch (rax rcx rdx r9 r10 r11 r14 r15)


def raw(text, prefixes=b'', rexw=False, p66=None):
    """`text` assembled with legacy `prefixes` in front, REX.W set, and for ADOX a 66 before or after its F3 (`p66`)."""
    b = bytearray(ref.assemble(text))
    if rexw:
        at = b.index(0x0f)
        if at and 0x40 <= b[at - 1] <= 0x4f: b[at - 1] |= 8
        else: b.insert(at, 0x48)
    if p66: b.insert(b.index(0xf3) + (p66 == 'after'), 0x66)
    return '.byte ' + ', '.join(f'{x:#04x}' for x in bytes(prefixes) + bytes(b))


class Prog(g.Prog):
    def adx(self, model=None, raw_ok=True):
        r = self.rng; op = r.choice(['adcx', 'adox']); w = r.choice([32, 64]); d = r.choice(SCR)
        self.need_flags(flags.CF if op == 'adcx' else flags.OF)
        k = r.randrange(10)
        if k < 5: s = r.choice(SCR + PTRS + [8, d]); src = NAMES[w][s]
        elif k < 8: src = self.mem(w)
        else: src = f'{g.SIZE[w]} ptr [{NAMES[32][r.choice(PTRS)]} + {r.randrange(-0x200, 0x200)}]'   # 0x67: 32-bit address
        if model is not None:
            if k >= 5: src = f'{g.SIZE[w]} ptr [{DATA + r.randrange(0, g.REGION - 8)}]'
            model.append((op, w, d, None if 'ptr' in src else s, int(src.split('[')[1][:-1]) if 'ptr' in src else None))
        text = f'{op} {NAMES[w][d]}, {src}'
        if raw_ok and r.random() < 0.25 and '{' not in text:
            text = raw(text, bytes(r.sample([0x3e, 0x26, 0x2e, 0x36], r.randrange(3))), rexw=w == 64 and r.random() < 0.5,
                       p66=r.choice([None, 'before', 'after']) if op == 'adox' else None)
            self.lines.append(text); a = {'op': op, 'width': w}; self.undef = flags.step(a, self.undef); self.insns.append(a)
        else: self.emit(text)


def program(name, rng, family):
    r = random.Random(rng.getrandbits(64)); gpr, rf = g.initial(r); p = Prog(name, r); model = []
    rf = r.choice([0, 0x1, 0x800, 0x801, 0x8d5, 0xd4, 0x41])
    if family == 'model':
        for _ in range(24):
            if r.random() < 0.2: m = r.choice(['stc', 'clc', 'cmc']); p.emit(m); model.append((m,))
            else: p.adx(model, raw_ok=False)
        return g.finish(p, gpr, rf, r) | {'cpu': CPU, 'adx': model}
    if family == 'chain':
        # rdx = multiplier; rsi/rdi point into DATA (a[], b[]); two chains: ADCX on r9/r10, ADOX on r11/r14
        p.emit(f'add {NAMES[64][r.choice(SCR)]}, {NAMES[64][r.choice(SCR)]}')
        for i in range(6):
            p.emit(f'mulx r15, rax, qword ptr [rsi + {8 * i}]', op='mulx')
            p.emit('adcx r9, rax'); p.emit('adox r10, r15')
            p.emit(f'adcx r11, qword ptr [rdi + {8 * i}]'); p.emit(f'adox r14, qword ptr [rdi + {8 * i + 8}]')
            if r.random() < 0.5: p.emit('pushfq', op='pushf'); p.emit(f'pop {NAMES[64][r.choice([0, 1])]}')
            if r.random() < 0.3: p.emit(f'adcx {NAMES[32][r.choice(SCR)]}, {NAMES[32][r.choice(SCR)]}')
        p.emit('pushfq', op='pushf'); p.emit('pop rcx')
        return g.finish(p, gpr, rf, r) | {'cpu': CPU}
    for _ in range(28):
        if r.random() < 0.45: p.adx()
        else:
            k = r.random()
            if k < 0.1: p.emit(r.choice(['stc', 'clc', 'cmc']))
            elif k < 0.15: p.emit('sahf')
            else: p.one()
    return g.finish(p, gpr, rf, r) | {'cpu': CPU}


def check(programs, expected):
    """Replays the `adx-model-*` programs (SDM Vol.2 ADCX/ADOX Operation); the other flags keep their initial values."""
    bad = []
    for p in programs:
        if 'adx' not in p: continue
        mem = bytes.fromhex(p['memory'][0]['data']); want = expected[p['name']]['qemu']
        x = [int(v, 16) for v in p['gpr']]; rf = int(p['rflags'], 16); cf, of = rf & 1, rf >> 11 & 1
        for t in p['adx']:
            if t[0] == 'stc': cf = 1
            elif t[0] == 'clc': cf = 0
            elif t[0] == 'cmc': cf ^= 1
            else:
                op, w, d, s, ea = t; m = (1 << w) - 1
                v = x[s] & m if s is not None else int.from_bytes(mem[ea - DATA:ea - DATA + w // 8], 'little')
                res = (x[d] & m) + v + (cf if op == 'adcx' else of)
                x[d] = res & m                      # a 32-bit destination is zero-extended
                if op == 'adcx': cf = res >> w
                else: of = res >> w
        got = [int(v, 16) for v in want['gpr']]; grf = int(want['rflags'], 16)
        wrf = rf & ~0x801 | cf | of << 11
        if got != x: bad.append(f"{p['name']}: registers model {[hex(v) for v in x]} vs reference {want['gpr']}")
        elif (grf ^ wrf) & flags.ARITH: bad.append(f"{p['name']}: rflags model {wrf:#x} vs reference {grf:#x}")
    return bad


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261104)
    ap.add_argument('--check', type=pathlib.Path, metavar='EXPECTED')
    a = ap.parse_args(); rng = random.Random(a.seed)
    if a.check:
        exp = {r['name']: r for r in json.loads(a.check.read_text())['results']}
        progs = json.loads(a.output.read_text())['programs']
        bad = check(progs, exp)
        print(*bad[:10], f"{sum('adx' in p for p in progs)} model programs, {len(bad)} mismatches", sep='\n')
        sys.exit(1 if bad else 0)
    progs = [program(f'adx-model-{i:03d}', rng, 'model') for i in range(16)]
    progs += [program(f'adx-chain-{i:03d}', rng, 'chain') for i in range(16)]
    progs += [program(f'adx-mix-{i:03d}', rng, 'mix') for i in range(24)]
    clang = subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    a.output.write_text(json.dumps({'generator': {'script': 'tools/reference/gen_adx.py', 'seed': a.seed, 'assembler': clang},
                                    'programs': progs}, indent=1) + '\n')
    print(f'{len(progs)} programs -> {a.output}')

if __name__ == '__main__': main()
