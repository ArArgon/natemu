#!/usr/bin/env python3
"""Census of words in fexc slice1 code that read the code area as data, or need a frame field or the native stack.

    python3 -B literals.py FEXC OUT.json PROGRAMS.json [PROGRAMS.json ...]

Same block walk as census.py. Per word, by exact encoding:
- `ldr*` literal (integer, SIMD, prfm): reads the code area. Each is matched to a guest-RIP relocation or not.
- `adr`/`adrp`: forms a code-area address (not itself a data access).
- x28-based accesses, by frame offset and direction.
- sp-based accesses and `sp` writes; `blr`/`bl`/`ret`.
A word inside a guest-RIP literal slot is data and skipped."""
import json, sys, collections, tempfile, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from census import compile_block

def sp_near(code, at):                    # sp-based ldr/str within 4 words of a blr (the helper call's lr push and pop)
    n = 0
    for x in range(at - 16, at + 20, 4):
        w = int.from_bytes(code[x:x + 4], 'little') if 0 <= x < len(code) else 0
        n += (w & 0x3b000000 == 0x39000000 or w & 0x3b200000 == 0x38000000) and w >> 5 & 31 == 31
    return int(n == 2)

def scan(j, c, ex):
    c['spill_bytes_nonzero'] += j['code']['layout']['spill_bytes'] != 0
    code = bytes.fromhex(j['code']['hex']); end = j['code']['layout']['code_region_end']
    lits = {}
    for r in j['code']['relocations']:
        if r['kind'] == 'guest_rip_literal':
            lits[r['offset']] = r; lits[r['offset'] + 4] = r
    for at in range(4, end, 4):
        if at in lits: continue
        w = int.from_bytes(code[at:at + 4], 'little')
        if w & 0x3b000000 == 0x18000000:                       # LDR (literal), all forms
            t = at + 4 * ((w >> 5 & 0x7ffff) - (1 << 19 if w >> 23 & 1 else 0))
            kind = {0: 'w', 1: 'x', 2: 'ldrsw', 3: 'prfm'}[w >> 30] if not w >> 26 & 1 else 'simd'
            ok = t in lits and lits[t]['offset'] == t
            c[f'literal_{kind}_{"guest_rip" if ok else "OTHER"}'] += 1
            if not ok: ex.append((j['guest']['rip'] if 'rip' in j['guest'] else '?', at, hex(w)))
        elif w & 0x9f000000 == 0x10000000: c['adr'] += 1
        elif w & 0x9f000000 == 0x90000000: c['adrp'] += 1
        elif w & 0x3b000000 == 0x39000000 and w >> 5 & 31 == 28:     # ldr/str unsigned imm, base x28
            size = 1 << (w >> 30); off = (w >> 10 & 0xfff) * size; load = w >> 22 & 1
            c[f'x28_{"ld" if load else "st"}_{off}'] += 1
        elif w & 0x3b000000 == 0x39000000 and w >> 5 & 31 == 31 or w & 0x3b200000 == 0x38000000 and w >> 5 & 31 == 31:
            c['sp_access'] += 1
        elif w & 0xff8003ff == 0xd100001f or w & 0xff8003ff == 0x9100001f: c['sp_write'] += 1
        elif w & 0xfffffc1f == 0xd63f0000: c['blr'] += 1; c['blr_with_2_sp_accesses'] += sp_near(code, at)
        elif w & 0xfc000000 == 0x94000000: c['bl'] += 1
        elif w == 0xd65f03c0: c['ret'] += 1

def main():
    fexc, out, *corpora = sys.argv[1:]
    total = collections.Counter(); ex = []
    with tempfile.TemporaryDirectory() as d:
        for path in corpora:
            progs = json.load(open(path)); progs = progs['programs'] if 'programs' in progs else progs['benchmarks']
            for p in progs:
                code = bytes.fromhex(p['code']); r = p.get('rip', 0x100000); base = int(r, 16) if isinstance(r, str) else r
                todo, seen = [base], set()
                while todo:
                    rip = todo.pop()
                    if rip in seen or not base <= rip < base + len(code): continue
                    seen.add(rip)
                    j = compile_block(fexc, code[rip - base:], rip, d)
                    total['status:' + j['status']] += 1
                    if j['status'] != 'ok' or not j['code']['hex']: continue
                    total['blocks'] += 1; scan(j, total, ex)
                    total['guest_rip_literal_slots'] += sum(r['kind'] == 'guest_rip_literal' for r in j['code']['relocations'])
                    total['relocation_kinds:' + ','.join(sorted({r['kind'] for r in j['code']['relocations']}))] += 1
                    for r in j['code']['relocations']:
                        if r['kind'] == 'guest_rip_literal': todo.append(int(r['guest_rip'], 16))
    json.dump({'total': total, 'other_literals': ex[:50]}, open(out, 'w'), indent=1)
    for k, v in sorted(total.items()): print(f'{k} {v}')
    print('literal loads not of a guest-RIP slot:', len(ex), ex[:5])

if __name__ == '__main__':
    main()
