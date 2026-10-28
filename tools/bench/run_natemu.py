#!/usr/bin/env python3
"""Steady-state ns per guest instruction of the benchmarks (benchmarks.json) under natemu: a measurement, no tuning.

    tools/with-env python3 tools/bench/run_natemu.py --fexc FEXC --out DIR [--reps 5] [--scale 1.0] [--bench int,mem64]
                                                       [--max-block N]

Each benchmark runs through the program driver (`natemu-host RUNTIME run-program`) at N and 2N iterations (N = n_default x
--scale), interleaved, --reps times after one discarded warm-up round. Per repetition the slope (t(2N) - t(N)) / (insns(2N)
- insns(N)) of process wall time is the ns per guest instruction (startup, compiles and the VM round trips fall into the
intercept). Every run's final GPRs must equal the native model (build_bench.model) and it must stop at the program end; the
runs at N and 2N must compile the same blocks, make the same resident entries and links (steady state: per iteration only
generated code and, for computed exits, the dispatcher's L1 hits). Writes DIR/results.json; prints a table next to QEMU's system-TCG numbers."""
import argparse, json, pathlib, statistics, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE.parent / 'reference'))
import build_bench as bench, run_slice1_vm as vm, ref  # noqa: E402

# qemu-system-x86_64 TCG on the same host, steady-state ns/insn (S2p, bench-qemu-20261027-01).
QEMU = {'int': 0.200, 'mem64': 0.346, 'mem4096': 0.398, 'call': 0.744, 'branchy': 0.597, 'mixed': 0.283}
STATS = ('blocks', 'fast', 'compiles', 'linker', 'links', 'linked', 'flushes', 'retries')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--reps', type=int, default=5)
    ap.add_argument('--scale', type=float, default=1.0)
    ap.add_argument('--bench')
    ap.add_argument('--max-block')
    a = ap.parse_args()
    host, runtime = vm.build()
    a.out.mkdir(parents=True, exist_ok=True)
    model_exe = a.out / 'model'
    bench.build_model(model_exe)
    benches = [b for b in bench.load()['benchmarks'] if not a.bench or b['name'] in a.bench.split(',')]
    cases = {}
    for b in benches:
        n = max(1, round(b['n_default'] * a.scale))
        for k in (n, 2 * n):
            work = a.out / 'work' / f"{b['name']}-{k}"
            work.mkdir(parents=True, exist_ok=True)
            (work / 'program.txt').write_text(vm.description(bench.program(b, k)))
            cases[(b['name'], k)] = (work, *bench.model(b, k, model_exe))

    def run(name, k):
        work, gpr, insns = cases[(name, k)]
        args = [host, runtime, 'run-program', a.fexc, work / 'program.txt', work, '--watchdog-ms', '600000']
        args += [a.max_block] if a.max_block else []
        t0 = time.perf_counter()
        r = subprocess.run(args, capture_output=True, text=True)
        dt = time.perf_counter() - t0
        if r.returncode != 0:
            sys.exit(f'{name} N={k}: host error {r.stderr.strip()[-400:]}')
        s = json.loads(r.stdout)
        got = [ref.num(v) for v in s['gpr']]
        if s['stop'] != 'reached' or got != gpr:
            sys.exit(f"{name} N={k}: stopped {s['stop']} {s['reason']}; GPRs {'match' if got == gpr else 'differ from the model'}")
        return dt, {x: s['stats'][x] for x in STATS}

    times = {key: [] for key in cases}
    stats = {}
    for rep in range(a.reps + 1):
        for (name, k) in cases:
            dt, st = run(name, k)
            if rep:
                times[(name, k)].append(dt)
            stats[(name, k)] = st
    results = []
    print(f"{'bench':9} {'N':>11} {'insns/N':>8} {'ns/insn':>8} {'range%':>7} {'QEMU':>6} {'ratio':>6}  counters at N (= at 2N)")
    for b in benches:
        n = max(1, round(b['n_default'] * a.scale))
        (_, _, i1), (_, _, i2) = cases[(b['name'], n)], cases[(b['name'], 2 * n)]
        slopes = [1e9 * (t2 - t1) / (i2 - i1) for t1, t2 in zip(times[(b['name'], n)], times[(b['name'], 2 * n)])]
        med = statistics.median(slopes)
        # Computed exits (ret) take the dispatcher every iteration: its hits grow with N; nothing else may.
        steady = all(stats[(b['name'], n)][x] == stats[(b['name'], 2 * n)][x] for x in STATS if x != 'fast')
        r = {'name': b['name'], 'n': n, 'insns': [i1, i2], 'ns_per_insn': {'median': med, 'min': min(slopes), 'max': max(slopes),
             'range_pct': 100 * (max(slopes) - min(slopes)) / med, 'all': slopes}, 'qemu_ns_per_insn': QEMU.get(b['name']),
             'times_s': {'n': times[(b['name'], n)], '2n': times[(b['name'], 2 * n)]},
             'stats': {'n': stats[(b['name'], n)], '2n': stats[(b['name'], 2 * n)]}, 'steady': steady}
        results.append(r)
        q = QEMU.get(b['name'])
        print(f"{b['name']:9} {n:11} {(i2 - i1) / n:8.3f} {med:8.4f} {r['ns_per_insn']['range_pct']:7.2f} {q or 0:6.3f} "
              f"{med / q if q else 0:6.2f}  {'' if steady else 'NOT STEADY '}{stats[(b['name'], n)]}")
    (a.out / 'results.json').write_text(json.dumps({'reps': a.reps, 'scale': a.scale, 'max_block': a.max_block,
                                                     'results': results}, indent=1) + '\n')
    sys.exit(0 if all(r['steady'] for r in results) else 1)


if __name__ == '__main__': main()
