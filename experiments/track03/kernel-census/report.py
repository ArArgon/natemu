#!/usr/bin/env python3 -I
"""report.py RAW_DIR OUT_DIR : census.json -> results.md, forms-kernel.tsv (decompressor + vmlinux), RAW_DIR/forms-initrd.tsv"""
import collections, json, os, sys
raw, out = sys.argv[1:3]
CL = ['admitted', 'SSE', 'AVX', 'x87', 'LOCK', 'string', 'system', 'int3', 'BMI', 'FS/GS', 'small-int', 'other', 'bad']
data = json.load(open(f'{raw}/census.json'))  # [group, name, [[sec, cls, mn, form, count, addr, why]]]
def rank(name, sec, addr):  # rough boot order: decompressor, kernel head (first 0x2000 of .text), .init.text, .text, alternatives
    if name.startswith('dec head 32'): return 0
    if name.startswith('dec head 64'): return 1
    if name.startswith('dec tail'): return 2
    a = int(addr, 16)
    if sec == '.text' and name == 'vmlinux': return 3 if a < 0xffffffff81002000 else 5
    return {'.init.text': 4, '.altinstr_replacement': 6, '.altinstr_aux': 6, '.exit.text': 7}.get(sec, 8)
def pct(n, t): return f'{100 * n / t:.1f}%' if t else '-'
M = []
w = M.append
# A. class counts per group / section
rows = collections.defaultdict(collections.Counter)
for g, n, rs in data:
    for sec, cls, mn, fm, c, addr, why in rs:
        if g == 'decompressor': key = n
        elif g == 'vmlinux': key = f'vmlinux {sec}'
        else: key = g
        rows[key][cls] += c
w('### A. Instruction counts by class (static, linear sweep)\n')
w('| Region | Total | ' + ' | '.join(CL) + ' | admitted share (int3 padding excluded) |'); w('|' + '---|' * (len(CL) + 3))
order = sorted(rows, key=lambda k: (0 if k.startswith('dec') else 1 if k.startswith('vmlinux') else 2, k))
for k in order:
    t = sum(rows[k].values()); w(f'| {k} | {t:,} | ' + ' | '.join(f'{rows[k][c]:,}' for c in CL) + f' | {pct(rows[k]["admitted"], t - rows[k]["int3"])} |')
# merge
def merged(pred):
    d = {}
    for g, n, rs in data:
        for sec, cls, mn, fm, c, addr, why in rs:
            if cls == 'admitted' or not pred(g, n, sec): continue
            k = (cls, mn, fm); r = rank(n, sec, addr); loc = (r, int(addr, 16))
            e = d.get(k)
            if e is None: d[k] = [c, loc, f'{n.split()[0]}:{sec}@{addr}', why, 1]
            else:
                e[0] += c; e[4] += 1
                if loc < e[1]: e[1], e[2] = loc, f'{n.split()[0]}:{sec}@{addr}'
    return d
kern = merged(lambda g, n, s: g in ('decompressor', 'vmlinux'))
with open(f'{out}/forms-kernel.tsv', 'w') as f:
    f.write('class\tmnemonic\toperand_shape\tcount\tfirst_use_region\tnote\n')
    for k, e in sorted(kern.items(), key=lambda kv: (CL.index(kv[0][0]), kv[1][1])):
        f.write(f'{k[0]}\t{k[1]}\t{k[2]}\t{e[0]}\t{e[2]}\t{e[3]}\n')
init = merged(lambda g, n, s: g.startswith('initrd'))
with open(f'{raw}/forms-initrd.tsv', 'w') as f:
    f.write('class\tmnemonic\toperand_shape\tcount\tfirst_use_region\tfiles\n')
    for k, e in sorted(init.items(), key=lambda kv: (CL.index(kv[0][0]), -kv[1][0])): f.write(f'{k[0]}\t{k[1]}\t{k[2]}\t{e[0]}\t{e[2]}\t{e[4]}\n')
def by_mn(d):
    m = {}
    for (cls, mn, fm), e in d.items():
        x = m.setdefault((cls, mn), [0, e[1], e[2], 0]); x[0] += e[0]; x[3] += 1
        if e[1] < x[1]: x[1], x[2] = e[1], e[2]
    return m
def table(title, d, top, order_first_use):
    w(f'\n### {title}\n'); w('Per class: distinct mnemonics (operand shapes merged), ordered by first use (region rank, then address) for the kernel and by count for the initramfs; shapes = distinct operand shapes (r register, m memory, i immediate, a absolute address, x/y/z xmm/ymm/zmm, sr segment register).\n')
    m = by_mn(d)
    for cls in CL[1:]:
        ms = [(mn, v) for (c, mn), v in m.items() if c == cls]
        if not ms: continue
        tot = sum(v[0] for _, v in ms); nf = sum(1 for k in d if k[0] == cls)
        w(f'\n**{cls}**: {tot:,} instructions, {len(ms)} distinct mnemonics, {nf} distinct mnemonic+shape forms.\n')
        w('| mnemonic | count | shapes | first use |'); w('|---|---|---|---|')
        for mn, v in sorted(ms, key=(lambda x: x[1][1]) if order_first_use else (lambda x: -x[1][0]))[:top]: w(f'| `{mn}` | {v[0]:,} | {v[3]} | {v[2]} |')
        if len(ms) > top: w(f'| ... {len(ms) - top} more, see TSV | | | |')
table('B. Kernel side (decompressor + vmlinux): refused mnemonics by class, in first-use order', kern, 40, True)
w('\n#### B2. Heaviest 15 refused forms (mnemonic + operand shape) in the kernel side\n'); w('| class | form | count | first use |'); w('|---|---|---|---|')
for k, e in sorted(kern.items(), key=lambda kv: -kv[1][0])[:15]: w(f'| {k[0]} | `{k[1]} {k[2]}` | {e[0]:,} | {e[2]} |')
w('\n#### B3. Kernel refused mnemonics in `.altinstr_replacement` (alternatives replacement side)\n')
alt = collections.Counter(); altfirst = {}
for g, n, rs in data:
    if g != 'vmlinux': continue
    for sec, cls, mn, fm, c, addr, why in rs:
        if sec == '.altinstr_replacement' and cls != 'admitted': alt[(cls, mn)] += c; altfirst.setdefault((cls, mn), addr)
w('| class | mnemonic | count |'); w('|---|---|---|')
for (cls, mn), c in alt.most_common(40): w(f'| {cls} | `{mn}` | {c} |')
table('C. Initramfs binaries: refused mnemonics by class (all 3 groups merged, ordered by count)', init, 25, False)
w('\n### D. Per-file class counts for the init chain (/init is a 456-byte shell script run by busybox; ld.so and libc load into every dynamic binary)\n')
w('| file | total | ' + ' | '.join(CL) + ' |'); w('|' + '---|' * (len(CL) + 2))
for g, n, rs in data:
    if g.startswith('initrd:init chain'):
        c = collections.Counter()
        for sec, cls, mn, fm, k, addr, why in rs: c[cls] += k
        w(f'| {n} | {sum(c.values()):,} | ' + ' | '.join(f'{c[x]:,}' for x in CL) + ' |')
open(f'{out}/results.md', 'w').write('\n'.join(M) + '\n')
print('\n'.join(M[:30]))
