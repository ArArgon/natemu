#!/usr/bin/env python3
"""QEMU system TCG over the memory-loop sweep (run.py SWEEP sets A and B): the mem4096 body with the stride and wrap mask
patched, run with tools/bench/run_qemu.py's own image builder, runner and fit. Final rax/r8/rsi checked against
tools/bench/attribution/mm.c (independent model); the other GPRs must keep their initial values (rcx ends at 0).

    python3 qemu_sweep.py OUT [REPS] [N]"""
import json, pathlib, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent; ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / 'tools/bench'))
import build_bench as bb, run_qemu as rq   # noqa: E402

def main():
    out = pathlib.Path(sys.argv[1]); reps = int(sys.argv[2]) if len(sys.argv) > 2 else 3; n = int(sys.argv[3]) if len(sys.argv) > 3 else 20_000_000
    out.mkdir(parents=True, exist_ok=True)
    mm = out / 'mm'; subprocess.run(['cc', '-O2', '-o', str(mm), str(ROOT / 'tools/bench/attribution/mm.c')], check=True)
    base = [x for x in bb.load()['benchmarks'] if x['name'] == 'mem4096'][0]
    points = [(s, 0xfffff) for s in (64, 1024, 2048, 4096, 4160, 8192, 16384)] + [(4096, p * 4096 - 1) for p in (16, 32, 64, 128)]
    benches, models = [], {}
    for stride, mask in points:
        c = bytearray.fromhex(base['code'])
        i = c.index(bytes.fromhex('4881c6')); c[i + 3:i + 7] = stride.to_bytes(4, 'little')
        j = c.index(bytes.fromhex('4881e6')); c[j + 3:j + 7] = mask.to_bytes(4, 'little')
        b = dict(base, name=f's{stride}-m{mask:x}', code=c.hex(), n_default=n); benches.append(b)
        for m in (1, 2):
            rax, r8, rsi = (int(x, 16) for x in subprocess.run([str(mm), str(n * m), str(stride), str(mask)], capture_output=True, text=True, check=True).stdout.split())
            g = bb.gprs(b, 0); g[0], g[8], g[6] = rax, r8, rsi
            models[(b['name'], n * m)] = (g, 8 * n * m)
    log = lambda s: print(s, flush=True)
    h0 = rq.host_state(); ns = rq.build_images(out, benches, [1, 2], 1.0)
    raw = rq.run_system(out, benches, [1, 2], ns, reps, models, log)
    res = {'host_start': h0, 'host_end': rq.host_state(), 'n': n, 'reps': reps, 'qemu': rq.sh([rq.QEMU_SYS, '--version']).splitlines()[0], 'rows': []}
    for (stride, mask), b in zip(points, benches):
        a = rq.analyse(b, [1, 2], ns, models, raw[b['name']])
        res['rows'].append({'stride': stride, 'mask': hex(mask), 'pages_4k': (mask + 1) // 4096, 'ns_per_iter': a['ns_per_iter']})
        print(f"stride {stride:6} pages {(mask + 1) // 4096:4}  ns/iter {a['ns_per_iter']['median']:.3f} (range {a['ns_per_iter']['range_pct']:.1f}%)")
    (out / 'qemu_sweep.json').write_text(json.dumps(res, indent=1) + '\n')

if __name__ == '__main__':
    main()
