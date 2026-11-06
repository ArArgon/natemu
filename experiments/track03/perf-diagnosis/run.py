#!/usr/bin/env python3
"""Performance diagnosis, measurement only (README.md). Needs FEXC (series10 fexc), cc/clang, the hypervisor entitlement.

    python3 run.py build OUT                       # build native, vmloop (signed), h2 into OUT/bin
    python3 run.py bench OUT                       # (a) native C, (b) FEX default profile, (c) slice1 blocks: host and VM; words/iter
    python3 run.py chase OUT [load|loadstore]             # dependent-load chase (hand-written arm64): load latency vs footprint, host vs VM
    python3 run.py sweep OUT [20000000 SETS]       # memory-loop stride/footprint sweep: native C, slice1 block on host and in VM
Results: OUT/bench.json, OUT/sweep.json (and a printed table)."""
import json, os, pathlib, re, statistics, struct, subprocess, sys, collections
HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
ATTR = ROOT / 'tools/bench/attribution'
sys.path.insert(0, str(ATTR)); sys.path.insert(0, str(ROOT / 'tools/bench'))
os.environ.setdefault('NATEMU_RAW_ROOT', str(ROOT.parent / 'natemu-raw'))
os.environ.setdefault('FEXC', '/Users/liaozping/orca/natemu-raw/fexc-native-20261026-01/series10/obj/fexc')
import build_native as bn, words as wd, build_bench as bb   # noqa: E402  (walk.F9 reads FEXC)
from dis2 import compile_block   # noqa: E402
N = {'int': 60_000_000, 'mem64': 60_000_000, 'mem4096': 6_000_000, 'call': 30_000_000, 'branchy': 20_000_000, 'mixed': 60_000_000}
PTR = {'mem64': {3}, 'mem4096': {3}, 'mixed': {6}}          # guest registers holding host/VM pointers (not compared)
FILL = {'mem64': 'zero', 'mem4096': 'zero', 'mixed': 'lcg'}


def sh(*a): return subprocess.run([str(x) for x in a], capture_output=True, text=True, check=True).stdout.strip()

def build(out):
    b = out / 'bin'; b.mkdir(parents=True, exist_ok=True)
    sh('xcrun', 'clang', '-arch', 'arm64', '-std=c11', '-Wall', '-Wextra', '-Werror', '-O2', HERE / 'vmloop.c', HERE / 'guest.S', ATTR / 'tramp2.s', '-framework', 'Hypervisor', '-o', b / 'vmloop')
    sh('codesign', '--force', '--sign', '-', '--entitlements', HERE / 'entitlements.plist', b / 'vmloop')
    sh('clang', '-O2', '-Wall', '-o', b / 'native', HERE / 'native.c')
    sh('cc', '-O2', '-o', b / 'h2', ATTR / 'h2.c', ATTR / 'tramp2.s')
    sh('cc', '-O2', '-o', b / 'mm', ATTR / 'mm.c')
    bb.build_model(b / 'model')
    (out / 'native.dis').write_text(sh('objdump', '-d', '--no-show-raw-insn', b / 'native'))

def ns(text):
    m = re.search(r'ns/iter ([0-9.]+) \(min ([0-9.]+) max ([0-9.]+)\)', text)
    if not m: raise SystemExit('no timing in output: ' + text[-300:])
    return {'median': float(m.group(1)), 'min': float(m.group(2)), 'max': float(m.group(3))}

def vm_regs(text): return [int(x, 16) for x in re.search(r'regs((?: [0-9a-f]+){16})', text).group(1).split()]

def image(out, tag, name, variant, n, stride=None, mask=None, bufbytes=None):
    """build_native image + cfg for a benchmark (optionally the mem loop with a patched stride and wrap mask)."""
    saved = [dict(x) for x in bn.B]
    try:
        if stride is not None:
            x = [x for x in bn.B if x['name'] == name][0]; c = bytearray.fromhex(x['code'])
            i = c.index(bytes.fromhex('4881c6')); c[i + 3:i + 7] = stride.to_bytes(4, 'little')
            j = c.index(bytes.fromhex('4881e6')); c[j + 3:j + 7] = mask.to_bytes(4, 'little'); x['code'] = c.hex()
        if variant == 'fex':
            b = [x for x in bn.B if x['name'] == name][0]
            extra = ['--no-tso', '--no-end-marker'] + (['--multiblock'] if name == 'branchy' else [])
            j = compile_block(os.environ['FEXC'], b['code'], 0x100000, 'default', extra)
            code, end, lits, text = bn.lines_of(j); code = bytearray(code[:end])
            bne = [o for o, t in text.items() if t.startswith('b.ne')][-1]       # the loop's back edge (dec rcx; jnz)
            struct.pack_into('<I', code, bne + 4, 0xf9400000 | (0xb88 // 8) << 10 | 28 << 5 | 1)   # ldr x1,[x28,#0xb88]
            struct.pack_into('<I', code, bne + 8, 0xd61f0020)                                     # br x1 (harness exit)
            img, base, entry, info = code + bytearray(64), {0x100000: 0}, 0x100000, {0x100000: dict(text=text)}
        else:
            b, img, base, entry, info = bn.build(name, variant)
        p = out / 'img' / f'{tag}.bin'; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(img)
        cfg = bn.config(name, b, entry, base, p, n) + (f'fill {FILL[name]}\n' if name in FILL else '') + (f'bufbytes {bufbytes}\n' if bufbytes else '')
        (out / 'img' / f'{tag}.cfg').write_text(cfg)
        return out / 'img' / f'{tag}.cfg', info
    finally:
        bn.B[:] = saved

def fex_words(name, text):
    """Native words per iteration of the default-profile loop (b.ne target .. b.ne). branchy: weighted by the model's branches."""
    bne = [o for o, t in text.items() if t.startswith('b.ne')][-1]
    top = int(re.search(r'0x([0-9a-f]+) <', text[bne]).group(1), 16)
    body = [o for o in sorted(text) if top <= o <= bne]
    if name != 'branchy':
        assert not any(re.match(r'(b\.|b\t|cb|tb)', text[o]) for o in body[:-1]), 'unexpected branch in the loop body'
        return len(body)
    # offsets fixed by the disassembly saved in bench.json: c..1c (5), odd: 20,24 / even: 28, 2c..34 (3), (edx&6)==0: 38..40, 44..58 (6)
    assert [text[o].split()[0] for o in (0x1c, 0x24, 0x34, 0x58)] == ['b.eq', 'b', 'b.ne', 'b.ne'], 'branchy layout changed'
    b = [x for x in bn.B if x['name'] == 'branchy'][0]
    rax, mul, add = int(b['gpr'][0], 16), int(b['gpr'][6], 16), int(b['gpr'][7], 16); tot = 0; K = 100000
    for _ in range(K):
        rax = (rax * mul + add) & (2**64 - 1); edx = (rax >> 40) & 0xffffffff
        tot += 5 + (2 if edx & 1 else 1) + 3 + (0 if edx & 6 else 3) + 6
    return tot / K

def slice1_words(name):
    b = [x for x in bn.B if x['name'] == name][0]; bl = bn.blocks(b); tot = 0
    for rip, w in wd.path(name, b).items():
        code, end, lits, text = bn.lines_of(bl[rip]); tot += w * len(wd.executed(text, bn.classify(text)))
    return tot + (12 if name == 'call' else 0)       # call: + dispatch_fast's 12 words per ret (README S2g table)

def bench(out):
    b_ = out / 'bin'; res = {}
    for name in N:
        b = [x for x in bb.load()['benchmarks'] if x['name'] == name][0]; r = res[name] = {'n': N[name]}
        r['insns_per_iter'] = b['insns_per_iter'] or 11.75
        model2n = bb.model(b, 2 * N[name], b_ / 'model')[0]
        t = sh(b_ / 'native', name, N[name]); r['a_native_c'] = ns(t); r['a_regs'] = t.split('regs')[1].strip()
        want = dict(zip(bb.REGS, bb.model(b, N[name], b_ / 'model')[0]))
        r['a_regs_match_model'] = all(want[k] == int(v, 16) for k, v in (kv.split('=') for kv in r['a_regs'].split()))
        for variant, key in (('fex', 'b_fex_default'), ('base', 'c_slice1')):
            if variant == 'fex' and name == 'call':
                r[key] = 'not measured: the default profile ends the block at the call (bl to an exit stub with FEX return-stack push), no in-block loop'; continue
            cfg, info = image(out, f'{name}-{variant}', name, variant, N[name])
            r[key] = {'host': ns(sh(b_ / 'h2', cfg))}
            t = sh(b_ / 'vmloop', cfg, 1, '4k', 7, '16k', 1); r[key]['vm'] = ns(t)
            got = vm_regs(t); skip = {4} | PTR.get(name, set())
            r[key]['vm_regs_match_model_2n'] = all(got[i] == model2n[i] for i in range(16) if i not in skip)
            r[key]['words_per_iter'] = fex_words(name, info[0x100000]['text']) if variant == 'fex' else slice1_words(name)
            r[key]['words_per_insn'] = r[key]['words_per_iter'] / r['insns_per_iter']
            if variant == 'fex': r[key]['disasm'] = [f'{o:#x}: {t}' for o, t in sorted(info[0x100000]['text'].items())]
        print(name, json.dumps({k: v for k, v in r.items() if k not in ('b_fex_default', 'c_slice1')}),
              {k: {kk: vv for kk, vv in r[k].items() if kk != 'disasm'} if isinstance(r[k], dict) else r[k] for k in ('b_fex_default', 'c_slice1')}, flush=True)
    res['host_load'] = sh('uptime')
    (out / 'bench.json').write_text(json.dumps(res, indent=1) + '\n')

# stride, wrap mask, bufbytes: A = 1 MiB footprint (the benchmark's), B = stride 4096 with fewer pages, C = 8 MiB footprint
SWEEP = ([('A', s, 0xfffff, None) for s in (64, 1024, 2048, 4096, 4160, 8192, 16384)] +
         [('B', 4096, p * 4096 - 1, None) for p in (16, 32, 64, 128)] +
         [('C', s, 0x7fffff, (8 << 20) + 16384) for s in (64, 4160)])   # h2: aligned_alloc needs a 16 KiB multiple
KNOBS = [('vm', (1, '4k', 7, '16k', 1)), ('vm_16k', (1, '16k', 7, '16k', 1)), ('vm_2m', (1, '2m', 7, '16k', 1))]
KNOBS_FULL = KNOBS + [('vm_tso0', (0, '4k', 7, '16k', 1)), ('vm_ipa4k', (1, '4k', 7, '4k', 1)), ('vm_el0', (1, '4k', 7, '16k', 0)),
                      ('vm_2m_tso0_el0', (0, '2m', 7, '16k', 0))]

def sweep(out, n=20_000_000, sets='ABC'):
    b_ = out / 'bin'; res = []
    for set_, stride, mask, buf in [x for x in SWEEP if x[0] in sets]:
        r = {'set': set_, 'stride': stride, 'mask': hex(mask), 'pages_4k': (mask + 1) // 4096, 'n': n}
        # mm.c (independent model, 4 MiB array) gives rax r8 rsi; set C (8 MiB) has no model check
        exp = [int(x, 16) for x in sh(b_ / 'mm', 2 * n, stride, mask).split()] if buf is None else None
        exp1 = [int(x, 16) for x in sh(b_ / 'mm', n, stride, mask).split()] if buf is None else None
        t = sh(b_ / 'native', 'mem4096', n, stride, mask); r['native_c'] = ns(t)
        r['native_c']['regs_ok'] = [int(x.split('=')[1], 16) for x in t.split('regs')[1].split()] == exp1 if exp1 else None
        cfg, _ = image(out, f'sw-{stride}-{mask:x}', 'mem4096', 'base', n, stride, mask, buf)
        r['host_slice1'] = ns(sh(b_ / 'h2', cfg))
        full = (set_, stride) in (('A', 4096), ('C', 4160), ('A', 4160), ('C', 64))
        for key, args in (KNOBS_FULL if full else KNOBS):
            t = sh(b_ / 'vmloop', cfg, *args); r[key] = ns(t); g = vm_regs(t)
            r[key]['regs_ok'] = [g[0], g[8], g[6]] == exp if exp else None
        print(json.dumps(r), flush=True); res.append(r)
    (out / f'sweep-{sets}.json').write_text(json.dumps({'rows': res, 'host_load': sh('uptime')}, indent=1) + '\n')

CHASE = [(4160, m) for m in (0xffff, 0x3ffff, 0xfffff, 0x3fffff, 0x7fffff)] + [(4096, 0xfffff), (4096, 0x3ffff), (64, 0xfffff), (16384, 0xfffff)]

EXIT = [0xf9400000 | (0xb88 // 8) << 10 | 28 << 5 | 1, 0xd61f0020]          # ldr x1,[x28,#0xb88]; br x1 (harness exit)
KINDS = {  # x4 = guest rax (offset), x10 = guest rsi (buffer), x7 = guest rcx (counter)
    'load': [0, 0xf8606800 | 4 << 16 | 10 << 5 | 4, 0xf10004e7, 0x54ffffc1] + EXIT,          # ldr x4,[x10,x4]; subs; b.ne
    'loadstore': [0, 0xf8606800 | 4 << 16 | 10 << 5 | 5, 0xf8206800 | 4 << 16 | 10 << 5 | 5,  # ldr x5,[x10,x4]; str x5,[x10,x4]
                  0xaa0503e4, 0xf10004e7, 0x54ffff81] + EXIT}                                 # mov x4,x5; subs; b.ne

def chase(out, n=10_000_000, kind='load'):
    """Dependent chase over offsets (fill chain:STRIDE:MASK); 'loadstore' also stores the loaded value back to the same address."""
    b_ = out / 'bin'; res = []
    img = out / 'img' / f'chase-{kind}.bin'; img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(struct.pack(f'<{len(KINDS[kind])}I', *KINDS[kind]) + bytes(64))
    for stride, mask in CHASE:
        cfg = out / 'img' / f'chase-{kind}-{stride}-{mask:x}.cfg'
        cfg.write_text(f'image {img}\nentry 4\nn {n}\nctr 1\nreg 6 buf+0\nfill chain:{stride}:{mask:x}\nbufbytes {max(mask + 1, 1 << 20)}\n')
        r = {'kind': kind, 'stride': stride, 'footprint_kib': (mask + 1) // 1024, 'cells': len({0} | {(k * stride) & mask for k in range((mask + 1) // 8)})}
        for key, args in (('host', (1, 'host', 7)), ('vm_4k', (1, '4k', 7)), ('vm_16k', (1, '16k', 7))):
            r[key] = ns(sh(b_ / 'vmloop', cfg, *args))
        print(json.dumps(r), flush=True); res.append(r)
    (out / f'chase-{kind}.json').write_text(json.dumps({'rows': res, 'host_load': sh('uptime')}, indent=1) + '\n')

if __name__ == '__main__':
    cmd, out = sys.argv[1], pathlib.Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
    if cmd == 'sweep' and len(sys.argv) > 4: sweep(out, int(sys.argv[3]), sys.argv[4])
    elif cmd == 'chase' and len(sys.argv) > 3: chase(out, 10_000_000, sys.argv[3])
    else: {'build': build, 'bench': bench, 'sweep': sweep, 'chase': chase}[cmd](out)
