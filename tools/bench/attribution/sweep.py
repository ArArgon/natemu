"""Native class-removed sweeps: sweep.py BENCH[,BENCH] VARIANT[,VARIANT] [PASSES]  (variants: build_native.py). Prints median ns/iter and the
delta to `base`; writes sweep_<benches>.json in the current directory. Needs NATEMU_RAW_ROOT (series9 fexc), nasm-free."""
import pathlib
import subprocess, sys, re, statistics, json
benches = sys.argv[1].split(','); variants = sys.argv[2].split(','); passes = int(sys.argv[3]) if len(sys.argv) > 3 else 3
res = {}
for p in range(passes):
    for b in benches:
        for v in variants:
            r = subprocess.run(['python3', str(pathlib.Path(__file__).resolve().parent / 'build_native.py'), b, v], capture_output=True, text=True)
            m = re.search(r'ns/iter ([0-9.]+)', r.stdout)
            if not m: print('FAIL', b, v, r.stdout, r.stderr[-300:]); continue
            res.setdefault((b, v), []).append(float(m.group(1)))
for b in benches:
    base = statistics.median(res[(b, 'base')]) if (b, 'base') in res else None
    for v in variants:
        if (b, v) in res:
            x = res[(b, v)]; med = statistics.median(x)
            print(f'{b:8} {v:22} {med:8.4f} ns/iter  min {min(x):.4f} max {max(x):.4f}' + (f'  delta {med-base:+.4f} ({100*(med-base)/base:+.1f}%)' if base else ''))
json.dump({f'{b}/{v}': x for (b, v), x in res.items()}, open('sweep_%s.json' % sys.argv[1].replace(',', '_'), 'w'))
