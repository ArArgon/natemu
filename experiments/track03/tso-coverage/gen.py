#!/usr/bin/env python3
"""Generate cells.S (arm64 test bodies, one set per cell) and cells.json (cell metadata)."""
import json, sys
from litmus import LITMUS, EX, ALL, forms, EX_FORMS, OFFS, PG_LITMUS, PG_XBASE

SWP = {1: 'swpalb', 2: 'swpalh', 4: 'swpal', 8: 'swpal'}
def K(w, byte): return int.from_bytes(bytes([byte]) * w, 'little')
def imm(v): return f'#{v:#x}'

def movi(b, r, v):
    b.e(f'movz {r}, #{v & 0xffff:#x}')
    for sh in (16, 32, 48):
        if (v >> sh) & 0xffff: b.e(f'movk {r}, #{(v >> sh) & 0xffff:#x}, lsl #{sh}')

def mn(kind, w, unscaled):
    base = {'st': 'st', 'ld': 'ld'}[kind] + ('ur' if unscaled else 'r')
    return base + {1: 'b', 2: 'h', 4: '', 8: ''}[w]
def reg(i, w): return ('x' if w == 8 else 'w') + str(i)

class Body:
    def __init__(s, cid, tid, w, off, unscaled):
        s.cid, s.tid, s.w, s.off, s.u = cid, tid, w, off, unscaled
        s.lines, s.handlers = [], []
    def e(s, text): s.lines.append('    ' + text)
    # address operand for loc in normal / alias mode
    def addr(s, loc, alias):
        i = 'xy'.index(loc)
        if alias: b = f'x{12 + i}'
        else: b = f'x{i}' if s.u else f'x{10 + i}'
        return f'[{b}, #{s.off}]' if s.u else f'[{b}]'
    def mem(s, kind, r, loc, alias):
        s.e(f'{mn(kind, s.w, s.u)} {r}, {s.addr(loc, alias)}')

def classify(b, rn, pos):
    # digit of rn (full x reg) at bit 2*pos of x7; temps x14,x15
    b.e('mov x14, #3')
    b.e(f'cmp {rn}, #0'); b.e('csel x14, xzr, x14, eq')
    b.e(f'cmp {rn}, x2'); b.e('mov x15, #1'); b.e('csel x14, x15, x14, eq')
    b.e(f'cmp {rn}, x3'); b.e('mov x15, #2'); b.e('csel x14, x15, x14, eq')
    b.e(f'orr x7, x7, x14, lsl #{2 * pos}')

def thread_body(cid, name, lit, tid, w, off, u):
    b = Body(cid, tid, w, off, u)
    ops = lit['threads'][tid]
    outs = lit['outputs']
    normal, alias = set(), set()
    for o in ops:
        if o[0] in ('st', 'swp', 'sth'): loc, h = o[1], len(o) > 3
        elif o[0] == 'ld': loc, h = o[2], len(o) > 3
        else: continue
        (alias if h else normal).add(loc)
    b.e('mov x7, #0')
    for loc in sorted(normal | alias):
        i = 'xy'.index(loc)
        if loc in normal and not u: b.e(f'add x{10 + i}, x{i}, #{off}')
        if loc in alias:
            b.e(f'add x{12 + i}, x{i}, #0x200, lsl #12')
            if not u and off: b.e(f'add x{12 + i}, x{12 + i}, #{off}')
    regs, nld, k = {}, 0, 0
    def newreg(r):
        nonlocal nld
        regs[r] = 4 + nld; nld += 1; return regs[r]
    i = 0
    while i < len(ops):
        o = ops[i]
        h = o[0] in ('st', 'ld') and len(o) > 3 or o[0] == 'dsb'
        if not h:
            if o[0] == 'st': b.mem('st', reg(2 if o[2] == 1 else 3, w), o[1], False)
            elif o[0] == 'swp':   # swpal of K1 to [x10]; old value to x9 (u=0 only: x10/x11 hold base+off)
                b.e(f'{SWP[w]} {reg(2, w)}, {reg(9, w)}, [x{10 + "xy".index(o[1])}]')
            elif o[0] == 'sth':   # two half-width stores, low half first
                hs = {2: ('strb', 1), 4: ('strh', 2), 8: ('str', 4)}[w]
                a = f'x{10 + "xy".index(o[1])}'
                b.e(f'{hs[0]} w2, [{a}]'); b.e(f'{hs[0]} w2, [{a}, #{hs[1]}]')
            else: b.mem('ld', reg(newreg(o[1]), w), o[2], False)
            i += 1; continue
        # gather handler group
        lab = f'Lc{cid}_t{tid}_h{k}'; k += 1
        b.e(f'adr x9, {lab}'); b.e('svc #0')
        hl = Body(cid, tid, w, off, u)
        while i < len(ops) and (ops[i][0] == 'dsb' or (ops[i][0] in ('st', 'ld') and len(ops[i]) > 3)):
            o = ops[i]
            if o[0] == 'dsb': hl.e('dsb ish')
            elif o[0] == 'st': hl.mem('st', reg(2 if o[2] == 1 else 3, w), o[1], True)
            else: hl.mem('ld', reg(newreg(o[1]), w), o[2], True)
            i += 1
        b.handlers.append((lab, hl.lines + ['    eret']))
    for pos, o in enumerate(outs):
        for r, ri in regs.items():
            if o == r: classify(b, f'x{ri}', pos)
    b.e('mov x0, x7'); b.e('ret')
    out = [f'_c{cid}_t{tid}:'] + b.lines
    for lab, ls in b.handlers: out += [f'{lab}:'] + ls
    return out

def fin_body(cid, lit, w, off, u):
    b = Body(cid, 0, w, off, u)
    b.e('mov x7, #0')
    for loc in 'xy':
        if any(o == 'mem:' + loc for o in lit['outputs']):
            i = 'xy'.index(loc)
            if not u: b.e(f'add x{10 + i}, x{i}, #{off}')
    for pos, o in enumerate(lit['outputs']):
        if o.startswith('mem:'):
            b.mem('ld', reg(4, w), o[4], False)
            classify(b, 'x4', pos)
    b.e('mov x0, x7'); b.e('ret')
    return [f'_c{cid}_fin:'] + b.lines

PATS = [(0xA5, 0x5A), (0x33, 0xCC)]
def atom_body(cid, tid, w, off, u, wm):
    # wm: plain (str/stur of the whole width), swpal, split (positive control: two half-width stores)
    b = Body(cid, tid, w, off, u)
    # x0 = region base (as X), x1 = iterations, x4 = flag base
    if u: addr = f'[x0, #{off}]'
    else:
        b.e(f'add x10, x0, #{off}'); addr = '[x10]'
    st, ld = mn('st', w, u), mn('ld', w, u)
    if tid < 1:
        a, c = PATS[tid]
        b.e('mov x8, x1')
        movi(b, 'x5', K(w, a)); movi(b, 'x6', K(w, c))
        b.e('mov x11, x1')
        lab = f'Lc{cid}_t{tid}_w'
        b.e(f'{lab}:')
        def one(r):
            if wm == 'plain': b.e(f'{st} {reg(r, w)}, {addr}')
            elif wm == 'swpal': b.e(f'{SWP[w]} {reg(r, w)}, {reg(9, w)}, [x10]')
            else:  # split: low half then high half, each half a single store of the (byte-uniform) pattern
                hs = {2: ('strb', 1), 4: ('strh', 2), 8: ('str', 4)}[w]
                b.e(f'{hs[0]} w{r}, [x10]'); b.e(f'{hs[0]} w{r}, [x10, #{hs[1]}]')
        for _ in range(4): one(5); one(6)
        b.e('subs x11, x11, #1'); b.e(f'b.ne {lab}')
        b.e('mov x7, #1'); b.e(f'add x12, x4, #{128 * tid}'); b.e('stlr x7, [x12]')
        b.e('lsl x0, x8, #3'); b.e('mov x1, #0'); b.e('mov x2, #0'); b.e('mov x3, #0'); b.e('mov x15, #0'); b.e('ret')
    else:
        movi(b, 'x5', K(w, 1))
        b.e('mov x13, #0'); b.e('mov x1, #0'); b.e('mov x2, #0'); b.e('mov x3, #0'); b.e('mov x14, #0'); b.e('mov x15, #0')
        lab = f'Lc{cid}_t{tid}_r'
        b.e(f'{lab}:')
        b.e('ldar x6, [x4]'); b.e('mov x7, #1')
        for j in range(8):
            b.e(f'{ld} {reg(8, w)}, {addr}')
            b.e('cmp x8, x14'); b.e('cinc x15, x15, ne'); b.e('mov x14, x8')
            b.e('and x9, x8, #0xff'); b.e('mul x9, x9, x5'); b.e('cmp x9, x8'); b.e(f'b.eq {lab}_{j}')
            b.e('add x1, x1, #1'); b.e('cmp x2, #0'); b.e('csel x2, x8, x2, eq'); b.e('mov x3, x8')
            b.lines.append(f'{lab}_{j}:')
        b.e('add x13, x13, #8')
        b.e(f'cbz x6, {lab}'); b.e(f'cbz x7, {lab}')
        b.e('mov x0, x13'); b.e('ret')
    return [f'_c{cid}_t{tid}:'] + b.lines

# Mixed-size cells.  x0 = X base; locations: 8 bytes at off (lo word at off, hi word at off+4).
# outputs: loads of 4 bytes -> digits (lo, hi); load of 8 bytes -> (d0: 0 zero,1 lo-only,2 hi-only,3 both; d1: 1 = other value)
MIX = {  # variant: (writer, reader)
    'st8-ld4lohi': ('st8', 'ld4lh'), 'st8-ld4hilo': ('st8', 'ld4hl'),
    'st4lohi-ld8': ('st4lh', 'ld8'), 'st4hilo-ld8': ('st4hl', 'ld8')}
MIX_CLS = {'al': 0, 'u16': 5, 'x16': 12, 'x64': 60}
def mix_thread(cid, variant, off, tid):
    b = Body(cid, tid, 4, off, 0)
    wr, rd = MIX[variant]
    b.e(f'add x10, x0, #{off}')
    if tid == 0:
        if wr == 'st8': b.e('orr x16, x2, x2, lsl #32'); b.e('str x16, [x10]')
        elif wr == 'st4lh': b.e('str w2, [x10]'); b.e('str w2, [x10, #4]')
        else: b.e('str w2, [x10, #4]'); b.e('str w2, [x10]')
        b.e('mov x0, #0'); b.e('ret')
    else:
        b.e('mov x7, #0')
        if rd == 'ld4lh': b.e('ldr w4, [x10]'); b.e('ldr w5, [x10, #4]')
        elif rd == 'ld4hl': b.e('ldr w5, [x10, #4]'); b.e('ldr w4, [x10]')
        if rd.startswith('ld4'):
            classify(b, 'x4', 0); classify(b, 'x5', 1)
        else:
            b.e('ldr x4, [x10]')
            b.e('lsl x15, x2, #32'); b.e('orr x16, x2, x15')
            b.e('mov x14, #0'); b.e('mov x9, #4')
            b.e('cmp x4, #0'); b.e('csel x9, xzr, x9, eq')
            for pat, d in (('x2', 1), ('x15', 2), ('x16', 3)):
                b.e(f'cmp x4, {pat}'); b.e(f'mov x13, #{d}'); b.e('csel x14, x13, x14, eq'); b.e('csel x9, xzr, x9, eq')
            b.e('orr x7, x14, x9')
        b.e('mov x0, x7'); b.e('ret')
    return [f'_c{cid}_t{tid}:'] + b.lines

def main(outdir):
    cells, text, table = [], [], []
    def add(name, kind, n, K1, K2, bodies, fin, meta):
        cid = len(cells)
        cells.append(dict(id=cid, name=name, kind=kind, n=n, **meta))
        table.append((cid, n, 0 if kind == 'lit' else 1, K1, K2, bodies, fin, meta.get('xbase', 0)))
    cid = 0
    def lit_cells(lname, lit, w, cls, off, u, xbase=0):
        name = f'{lname}.w{w}-{cls}.{"u" if u else "r"}'
        cid = len(cells)
        n = len(lit['threads'])
        for t in range(n): text.extend(thread_body(cid, name, lit, t, w, off, u))
        has_fin = any(o.startswith('mem:') for o in lit['outputs'])
        if has_fin: text.extend(fin_body(cid, lit, w, off, u))
        add(name, 'lit', n, K(w, 1), K(w, 2), n, has_fin,
            dict(litmus=lname, width=w, cls=cls, off=off, xbase=xbase, insn='ldur/stur' if u else 'ldr/str', ex=False))
    for lname, lit in LITMUS.items():
        for (w, cls, off) in forms():
            if lname == 'tear-split' and (cls not in ('al', 'u16') or w == 1): continue
            for u in ((0,) if lname in ('tear-swp', 'tear-split') else (0, 1)):
                lit_cells(lname, lit, w, cls, off, u)
        if lname in PG_LITMUS:
            for w in (2, 4, 8): lit_cells(lname, lit, w, 'pg', OFFS['x128'][w], 0, PG_XBASE)
    for lname, lit in EX.items():
        for (w, cls, off) in EX_FORMS:
            name = f'{lname}.w{w}-{cls}.r'
            cid = len(cells); n = len(lit['threads'])
            for t in range(n): text += thread_body(cid, name, lit, t, w, off, 0)
            add(name, 'lit', n, K(w, 1), K(w, 2), n, False,
                dict(litmus=lname, width=w, cls=cls, off=off, insn='ldr/str', ex=True))
    def atom_cell(w, cls, off, u, wm, xbase=0):
        name = f'atom.w{w}-{cls}.{"u" if u else "r"}' + ('' if wm == 'plain' else '.' + wm)
        cid = len(cells)
        for t in range(4): text.extend(atom_body(cid, t, w, off, u, wm))
        add(name, 'atom', 4, K(w, 1), K(w, 2), 4, False,
            dict(litmus='atom', width=w, cls=cls, off=off, wmode=wm, xbase=xbase, insn='ldur/stur' if u else 'ldr/str', ex=False))
    for (w, cls, off) in forms():
        for u in (0, 1): atom_cell(w, cls, off, u, 'plain')
    for w in (2, 4, 8): atom_cell(w, 'pg', OFFS['x128'][w], 0, 'plain', PG_XBASE)   # load/store crosses a 4 KiB page
    for w in (2, 4, 8): atom_cell(w, 'al', 0, 0, 'split')                                    # forced-tear positive control
    for (w, cls, off) in forms(): atom_cell(w, cls, off, 0, 'swpal')
    for w in (2, 4, 8): atom_cell(w, 'pg', OFFS['x128'][w], 0, 'swpal', PG_XBASE)
    for variant in MIX:
        for cls, off in MIX_CLS.items():
            name = f'mix.{variant}.{cls}'
            cid = len(cells)
            for t in range(2): text.extend(mix_thread(cid, variant, off, t))
            add(name, 'lit', 2, K(4, 1), K(4, 2), 2, False,
                dict(litmus='mix', variant=variant, width=8, cls=cls, off=off, insn='ldr/str', ex=False))
    s = ['// generated by gen.py; do not edit', '.include "guest.S"', '.text', '.p2align 3']
    s += text
    s += ['.p2align 3', '.globl _cell_table', '_cell_table:', f'    .quad {len(cells)}']
    for (cid, n, kind, K1, K2, nb, fin, xb) in table:
        q = [n, kind, K1, K2] + [f'_c{cid}_t{t} - _guest_start' if t < nb else 0 for t in range(4)]
        q += [f'_c{cid}_fin - _guest_start' if fin else 0, xb]
        s.append('    .quad ' + ', '.join(map(str, q)))
    s += ['.globl _guest_end', '.p2align 12', '_guest_end:']
    open(outdir + '/cells.S', 'w').write('\n'.join(s) + '\n')
    json.dump(cells, open(outdir + '/cells.json', 'w'), indent=0)
    print(len(cells), 'cells')

if __name__ == '__main__': main(sys.argv[1])
