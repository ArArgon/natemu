#!/usr/bin/env python3
"""Build a native-harness image of a benchmark's linked FEX blocks (series9, slice1 profile), with one check class removed.

    build_native.py BENCH VARIANT [run]   VARIANT: base nostop nohdr noprologue skip3 noguard nosplit nochecks nochecks-noprologue
                                          nochecks-skip3 fexonly nodisp
Classes are found by pattern in the disassembly of each block (see classify)."""
import json, re, subprocess, sys, pathlib, struct
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from dis2 import compile_block, words, disasm
from walk import blocks, F9
NOP = 0xd503201f
B = json.load(open(HERE.parent / 'benchmarks.json'))['benchmarks']

def lines_of(j):
    code, end, lay = words(j)
    lits = [r['offset'] for r in j['code']['relocations'] if r['kind'] == 'guest_rip_literal']
    out = {}
    for l in disasm(code, end, lits):
        m = re.match(r'\s+([0-9a-f]+):\s+(.*)', l)
        out[int(m.group(1), 16)] = m.group(2).strip()
    return code, end, lits, out

def classify(text):
    """offset -> class for the words of one block: hdr, stop, guard, split, link (first word of every exit tail)."""
    cls = {}
    for off, t in text.items():
        if t.startswith('adr\tx0') or t.startswith('str\tx0, [x28]'): cls[off] = 'hdr'
        if t.startswith('str\txzr, [x28, #0xec0]'): cls[off] = 'stop'
    for off, t in text.items():
        m = re.match(r'(tbnz|cbnz)\t(\S+), (#0x\S+, )?0x([0-9a-f]+) <', t)
        if m and text.get(int(m.group(4), 16), '').startswith('brk'):
            if '#0x3f' in t:                                   # guard: [add/sub x1,...] eor x0,R,R,lsl #16 ; tbnz x0,#63
                cls[off] = 'guard'; e = text[off - 4]
                assert e.startswith('eor\tx0, ') and 'lsl #16' in e, (off, e)
                cls[off - 4] = 'guard'
                if e.split()[2].rstrip(',') == 'x1' and re.match(r'(add|sub)\tx1, ', text[off - 8]): cls[off - 8] = 'guard'
            elif 'w0, #0xc' in t:                              # split: add x0,x1,#N ; eor x0,x0,x1 ; tbnz w0,#12
                assert text[off - 4].startswith('eor\tx0, x0, x1') and text[off - 8].startswith('add\tx0, x1, #'), (off, t)
                cls[off] = cls[off - 4] = cls[off - 8] = 'split'
            else: raise SystemExit(f'unknown check at {off:#x}: {t}')
    for off, t in text.items():
        if t.startswith('ldr\tx0, 0x') and text.get(off + 4, '').startswith('str\tx0, [x28, #0x18]'): cls[off] = 'link'
    return cls

def build(name, variant):
    b = [x for x in B if x['name'] == name][0]
    bl = blocks(b)
    info = {}
    for rip, j in bl.items():
        code, end, lits, text = lines_of(j)
        info[rip] = dict(j=j, code=bytearray(code[:end]), lits=lits, text=text, cls=classify(text))
    # layout
    base, at = {}, 0
    for rip in sorted(info):
        base[rip] = at; at += (len(info[rip]['code']) + 63) // 64 * 64
    img = bytearray(at + 64)
    def w(rip, off, word): struct.pack_into('<I', info[rip]['code'], off, word)
    def b_to(src, dst): return 0x14000000 | (((dst - src) // 4) & 0x03ffffff)
    skip = 12 if variant in ('skip3', 'nochecks-skip3') else 4
    for rip, d in info.items():
        for r in d['j']['code']['relocations']:
            if r['kind'] != 'guest_rip_literal': continue
            tgt = int(r['guest_rip'], 16)
            if tgt not in info: continue
            ldr = [o for o, t in d['text'].items() if t.startswith('ldr\tx0, 0x') and int(re.search(r'0x([0-9a-f]+) <', t).group(1), 16) == r['offset']]
            if r['offset'] >= len(d['code']): continue
            assert len(ldr) == 1, (rip, r)
            w(rip, ldr[0], b_to(base[rip] + ldr[0], base[tgt] + skip))
    # variant transforms
    for rip, d in info.items():
        kill = lambda *cs: [o for o, c in d['cls'].items() if c in cs]
        offs = []
        if variant in ('nostop',): offs = [o for o, c in d['cls'].items() if c == 'stop']
        if variant in ('nohdr',): offs = kill('hdr')
        if variant in ('noprologue', 'nochecks-noprologue'): offs = kill('hdr', 'stop')
        if variant in ('noguard', 'nochecks', 'nochecks-noprologue', 'nochecks-skip3'):
            offs += kill('guard')
        if variant in ('nosplit', 'nochecks', 'nochecks-noprologue', 'nochecks-skip3'): offs += kill('split')
        if variant in ('nochecks-skip3',): pass
        for o in set(offs):
            # an add/sub x1 shared with a split check stays when only the guard is removed
            if variant == 'noguard' and d['cls'][o] == 'guard' and re.match(r'(add|sub)\tx1, ', d['text'][o]) and d['cls'].get(o + 12) == 'split': continue
            w(rip, o, NOP)
        if variant == 'nodisp':   # a single return site: the computed exit goes straight to it
            for o, t in d['text'].items():
                if t.startswith('ldr\tx1, [x28, #0xb68]'):
                    w(rip, o, NOP); assert d['text'][o + 4].startswith('br\tx1'); w(rip, o + 4, b_to(base[rip] + o + 4, base[0x100017] + 4))
    for rip, d in info.items(): img[base[rip]:base[rip] + len(d['code'])] = d['code']
    entry = {'call': 0x10000f}.get(name, 0x100000)
    return b, img, base, entry, info

def fexonly(name):
    """The default-profile (pure FEX, no TSO) block with its in-block loop, ending through the harness exit."""
    b = [x for x in B if x['name'] == name][0]
    j = compile_block(F9, b['code'], 0x100000, 'default', ['--no-tso', '--no-end-marker'])
    code, end, lits, text = lines_of(j)
    code = bytearray(code[:end])
    bn = [o for o, t in text.items() if t.startswith('b.ne')][-1]
    struct.pack_into('<I', code, bn + 4, 0xf9445f81 | 0)   # ldr x1,[x28,#0x8b8]? replaced below
    struct.pack_into('<I', code, bn + 4, 0xf9400000 | (0xb88 // 8) << 10 | 28 << 5 | 1)   # ldr x1,[x28,#0xb88]
    struct.pack_into('<I', code, bn + 8, 0xd61f0020)       # br x1
    return b, bytearray(code) + bytearray(64), {0x100000: 0}, 0x100000, text

def config(name, b, entry, base, image, n, l1=True):
    g = b['gpr']; lines = [f'image {image}', f'entry {base[entry] + 4 if isinstance(base, dict) and entry in base else 4}', f'n {n}', 'ctr 1']
    # initial registers: pointers for guest addresses, values otherwise
    ptr = {'int': {}, 'mem64': {3: 'buf+0'}, 'mem4096': {3: 'buf+0'}, 'call': {}, 'branchy': {}, 'mixed': {6: 'buf+0'}}[name]
    for i, v in enumerate(g):
        if i == 1: continue
        if i == 4: lines.append(f'reg 4 stack+0x800'); continue
        if i in ptr: lines.append(f'reg {i} {ptr[i]}')
        elif int(v, 16): lines.append(f'reg {i} {v}')
    if name == 'call' and l1: lines.append(f'l1 0x100017 {base[0x100017] + 4}')
    return '\n'.join(lines) + '\n'

if __name__ == '__main__':
    name, variant = sys.argv[1:3]
    n = {'int': 60_000_000, 'mem64': 60_000_000, 'mem4096': 6_000_000, 'call': 30_000_000, 'branchy': 20_000_000, 'mixed': 60_000_000}[name]
    n = int(n * float(sys.argv[3])) if len(sys.argv) > 3 else n
    import os, tempfile
    out = pathlib.Path(os.environ.get('ATTR_OUT') or tempfile.gettempdir() + '/natemu-attr'); out.mkdir(exist_ok=True)
    if not (out / 'h2').exists():
        subprocess.run(['cc', '-O2', '-o', str(out / 'h2'), str(HERE / 'h2.c'), str(HERE / 'tramp2.s')], check=True)
    if os.environ.get('PAGES'):     # mem4096 with a wrap mask of PAGES 4 KiB pages
        for x in B:
            if x['name'] == 'mem4096':
                c = bytearray.fromhex(x['code']); j = c.index(bytes.fromhex('4881e6')); c[j + 3:j + 7] = (int(os.environ['PAGES']) * 4096 - 1).to_bytes(4, 'little')
                if os.environ.get('STRIDE'): i = c.index(bytes.fromhex('4881c6')); c[i + 3:i + 7] = int(os.environ['STRIDE']).to_bytes(4, 'little')
                x['code'] = c.hex()
    if variant == 'fexonly':
        b, img, base, entry, _ = fexonly(name)
    else:
        b, img, base, entry, _ = build(name, variant)
    (out / f'{name}-{variant}.bin').write_bytes(img)
    cfg = config(name, b, entry, base, out / f'{name}-{variant}.bin', n)
    cfg += 'shift %s\n' % os.environ.get('SHIFT', '0') if os.environ.get('SHIFT') else ''
    (out / f'{name}-{variant}.cfg').write_text(cfg)
    r = subprocess.run([str(out / 'h2'), str(out / f'{name}-{variant}.cfg')], capture_output=True, text=True)
    print(name, variant, r.stdout.strip(), r.stderr.strip()[:200])
