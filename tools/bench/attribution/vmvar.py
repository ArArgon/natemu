#!/usr/bin/env python3
"""Measurement-only VM variants of the memory benchmarks: array size (pages), base offset, stride. Slope N->2N like run_natemu.
usage: vmvar.py FEXC OUT REPS N STRIDE:PAGES:BASEOFF [...]"""
import json, pathlib, statistics, subprocess, sys, time
S = pathlib.Path(__file__).resolve().parent; ROOT = S.parent.parent.parent
sys.path.insert(0, str(ROOT / 'tools/bench')); sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools/reference'))
import build_bench as bench, run_slice1_vm as vm, ref
fexc, out, reps, n = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
specs = [tuple(int(x, 0) for x in s.split(':')) for s in sys.argv[5:]]
host, runtime = vm.build()
MM = pathlib.Path(__import__('tempfile').gettempdir()) / 'natemu-attr-mm'
subprocess.run(['cc', '-O2', '-o', str(MM), str(S / 'mm.c')], check=True)   # independent model of the loop's final registers
b = [x for x in bench.load()['benchmarks'] if x['name'] == 'mem64'][0]
cases = {}
for stride, pages, off in specs:
    size = pages * 4096 if stride >= 4096 else pages * 4096
    assert size & (size - 1) == 0
    code = bytes.fromhex(b['code'])
    # the stride immediate (add rsi, imm) and the wrap mask (and rsi, imm32) are patched; mem64 uses 4883c6 40 (imm8), mem4096 uses 4881c6 imm32
    c4096 = bytes.fromhex(bench.load()['benchmarks'][2]['code'])
    code = bytearray(c4096)
    i = code.index(bytes.fromhex('4881c6')); code[i + 3:i + 7] = stride.to_bytes(4, 'little')
    j = code.index(bytes.fromhex('4881e6')); code[j + 3:j + 7] = (size - 1).to_bytes(4, 'little')
    base = 0x200000 + off
    for k in (n, 2 * n):
        p = {'name': f'v{stride}-{pages}-{off}-{k}', 'code': bytes(code).hex(), 'rip': b['rip'], 'rflags': 0,
             'gpr': [f'0x{g:x}' for g in [0, k, 0, base, 0x301800, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]],
             'memory': [{'addr': 0x300000, 'data': '00' * 8192}, {'addr': base, 'data': '00' * size}], 'insns': b['insns']}
        work = out / p['name']; work.mkdir(parents=True, exist_ok=True)
        (work / 'program.txt').write_text(vm.description(p))
        exp = subprocess.run([str(MM), str(k), str(stride), str(size - 1)], capture_output=True, text=True).stdout.split()
        cases[(stride, pages, off, k)] = (work, [int(x, 16) for x in exp])
def run(key):
    work, exp = cases[key]
    t0 = time.perf_counter()
    r = subprocess.run([host, runtime, 'run-program', fexc, work / 'program.txt', work, '--watchdog-ms', '600000'], capture_output=True, text=True)
    dt = time.perf_counter() - t0
    s = json.loads(r.stdout); g = [ref.num(v) for v in s['gpr']]
    assert s['stop'] == 'reached' and [g[0], g[8], g[6]] == exp, (key, s['stop'], s['reason'], g[0], g[8], g[6], exp)
    return dt, s['stats']
res = {}
for rep in range(reps + 1):
    for stride, pages, off in specs:
        t = [run((stride, pages, off, k)) for k in (n, 2 * n)]
        if rep: res.setdefault((stride, pages, off), []).append(1e9 * (t[1][0] - t[0][0]) / n)
for (stride, pages, off), x in res.items():
    st = run((stride, pages, off, n))[1]
    print(f'stride {stride:5} pages {pages:4} base+{off:#x}: {statistics.median(x):7.3f} ns/iter (range {100*(max(x)-min(x))/statistics.median(x):.1f}%)  retries {st["retries"]}')
