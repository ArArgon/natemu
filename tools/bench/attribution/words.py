#!/usr/bin/env python3
"""Executed native words per benchmark iteration, by class (linked steady state)."""
import sys, json, collections
HERE = __import__('pathlib').Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from build_native import *

def executed(text, cls):
    """Words executed by one pass through the block: class of each, in order."""
    out = []
    for off in sorted(text):
        if off == 0: continue                                  # the header word (udf) is data
        t = text[off]
        c = cls.get(off, 'body')
        out.append((off, t, c))
        if c == 'link' or t.startswith('br\tx1'): break
    return out

def fex_body(name, rip, b):
    j = compile_block(F9, bytes.fromhex(b['code'])[rip - 0x100000:].hex(), rip, 'default', ['--no-tso', '--no-end-marker'])
    code, end, lits, text = lines_of(j)
    n = 0
    for off in sorted(text):
        if off < 0xc: continue
        t = text[off]
        if t.startswith(('b\t', 'br\t', 'bl\t', 'ldr\tx0, 0x')): break
        n += 1
    return n, text

def path(name, b):
    if name == 'branchy':
        rax, mul, add = int(b['gpr'][0], 16), int(b['gpr'][6], 16), int(b['gpr'][7], 16)
        cnt = collections.Counter(); N = 100000
        for _ in range(N):
            rax = (rax * mul + add) & (2**64 - 1); edx = (rax >> 40) & 0xffffffff
            seq = [0x100000] + ([0x10001b] if not edx & 1 else [0x100016, 0x10001e]) + ([0x100029] if edx & 6 else [0x100026])
            for r in seq: cnt[r] += 1
        return {r: c / N for r, c in cnt.items()}
    if name == 'call': return {0x10000f: 1, 0x100002: 1, 0x100017: 1}
    return {0x100000: 1}

if __name__ == '__main__':
    for name in (sys.argv[1:] or ['int', 'mem64', 'mem4096', 'call', 'branchy', 'mixed']):
        b = [x for x in B if x['name'] == name][0]
        bl = blocks(b); tot = collections.Counter(); fex = 0; detail = []
        for rip, weight in path(name, b).items():
            code, end, lits, text = lines_of(bl[rip]); cls = classify(text)
            ex = executed(text, cls)
            c = collections.Counter(x[2] for x in ex)
            fb, _ = fex_body(name, rip, b)
            c['body'] -= 0
            for k, v in c.items(): tot[k] += v * weight
            fex += fb * weight
            detail.append((rip, weight, dict(c), fb, len(ex)))
        insn = b['insns_per_iter']
        tot_words = sum(tot.values())
        print(f'== {name}: guest insns/iter {insn}, executed words/iter {tot_words:.2f}')
        print('   ', {k: round(v, 2) for k, v in sorted(tot.items())}, 'FEX default body words', round(fex, 2), '=> body minus FEX', round(tot['body'] - fex, 2))
        for d in detail: print('    block %#x w=%.3f classes=%s fexbody=%d words=%d' % d)
