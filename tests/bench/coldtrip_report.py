#!/usr/bin/env python3
"""The cost of one resident trip to guest code (docs/restructure/el0-sampler-plan.md, chunk 3; bench case
guest-coldtrip-profile).

    coldtrip_report.py run --out DIR --kernel K --initrd CPIO --fexc F --natemu N --runtime PERF_RESIDENT [--append WORDS]
    coldtrip_report.py report DIR

`run` boots the bench image's coldtrip program once (tools/bench/run.py --bench coldtrip --perf-log, 1 vCPU: two runs of
the program in the boot), then reports. `report` reads DIR/coldtrip.serial (the program's NATEMU-COLDTRIP lines),
DIR/coldtrip.perf (natemu's performance log: a `snap T NS...` line every 5 ms with each vCPU's time so far in each of the
`perf` resident's buckets) and DIR/bench.jsonl (run.py's record: the image's own check of the program's sums, and the
resident's count of CPUID instructions), and writes DIR/coldtrip.report.json.

Placing the phases. The program sleeps 100 ms before each phase, so the vCPU halts: in the snapshots every phase is one
busy stretch (intervals whose `halted` share is under IDLE) between halted ones. natemu's TSC counts the host clock's
nanoseconds from an offset fixed at the VM's start, so under the right offset each phase's TSC start and length meet
one stretch; the offset must place every phase of both runs, once (stretches no phase claims, such as the host writing
a console line during a gap, are left alone). A phase's buckets
are the snapshots' difference across its stretch and one halted interval on each side (so the edges' partly busy
intervals are in), less each bucket's rate while halted over the time in that window outside the phase.

What it computes. Per phase, guest-bucket ns per iteration (y) and trips per iteration (x = 1/K). Per footprint and
variant, in each counted round (round 0 of each run is a warm-up and left out), the least-squares slope of y on x:
the guest bucket's ns per trip. The guest bucket runs from just before `resume_exception` to the next entry's charge, so
    gate cost  = small footprint's slope, trip less control (the gate's fixed cost per trip)
    cold state = (large footprint's slope, trip less control) less the gate cost (the extra guest-code time per trip
                 when the guest's code spreads over a few hundred KiB)
and, the same way, the whole trip (every bucket but `halted`) per trip. Each is given per round and summarized over
the rounds (median, min, max, standard deviation); the slopes pooled over the rounds come with their standard errors.
A CPUID trip touches less resident state than a page fault, a system call or a dispatcher miss do: its cold state is a
lower bound for theirs.

The judge: both runs complete with every phase, each phase's trip count is ITERS / K (0 for k=0), every phase of a
footprint has the same work sum (and run.py's record verified, which compares it with the sum pinned at build time),
the resident ran at least as many CPUIDs as the trip phases and not many more, and every run's phases were placed in
the snapshots, once. Exit 0 when it holds, 1 when not (the problems are listed), run.py's status otherwise."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse, json, math, re, statistics, subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
NAME = 'coldtrip'
BUCKETS = ('start guest host halted mailbox link miss division helper stop irq abort place system tlb delivery revalidate '
           'shadow fetch shootdown lookup patch enter l1 enter_copy reuse pool relink').split()   # natemu_abi::execution::perf::NAMES
GUEST, HALTED = BUCKETS.index('guest'), BUCKETS.index('halted')
IDLE = 0.8                      # a snapshot interval is halted when `halted` took at least this share of it
EARLY_NS = 30_000_000           # how much earlier than its phase a stretch may start (host work merged in)
CPUID_KIND = '7'                # natemu_abi::execution::system::CPUID, in run.py's system_counts
CPUID_SLACK = 50_000             # CPUIDs the resident may run besides the trip phases' (kernel, every program's start)
PHASE = re.compile(r'NATEMU-COLDTRIP phase=(\d+) run=(\d+) round=(\d+) fp=(\w+) variant=(\w+) k=(\d+) iters=(\d+) trips=(\d+) '
                   r'tsc0=(\d+) tsc_ns=(\d+) sum=([0-9a-f]+)')


def parse_serial(text: str) -> dict:
    """{run: {'begin': {...}, 'phases': [...], 'end_ok': bool|None, 'sums': str|None}} from the program's lines."""
    runs = {}
    for line in text.replace('\r', '').splitlines():
        if 'NATEMU-COLDTRIP ' not in line: continue
        line = line[line.index('NATEMU-COLDTRIP '):]
        if m := PHASE.match(line):
            n, run, rnd, fp, var, k, it, tr, t0, ns, s = m.groups()
            runs.setdefault(int(run), {'phases': []})['phases'].append(
                dict(phase=int(n), run=int(run), round=int(rnd), fp=fp, variant=var, k=int(k), iters=int(it), trips=int(tr),
                     tsc0=int(t0), tsc_ns=int(ns), sum=s))
        elif line.startswith('NATEMU-COLDTRIP begin '):
            kv = dict(w.split('=', 1) for w in line.split()[2:] if '=' in w)
            runs.setdefault(int(kv['run']), {'phases': []})['begin'] = kv
        elif line.startswith('NATEMU-COLDTRIP end '):
            kv = dict(w.split('=', 1) for w in line.split()[2:] if '=' in w)
            runs.setdefault(int(kv['run']), {'phases': []})['end_ok'] = kv.get('ok') == '1'
    sums = re.findall(r'NATEMU-COLDTRIP sums (small=\S+ large=\S+)', text)
    for r, s in zip(sorted(runs), sums): runs[r]['sums'] = s
    return runs


def parse_snaps(text: str) -> tuple[list[int], list[list[int]], int]:
    """(times, per snapshot the buckets summed over vCPUs, vCPU count) from the performance log's snap lines."""
    ts, vals, n = [], [], 0
    for line in text.splitlines():
        if not line.startswith('snap '): continue
        w = list(map(int, line.split()[1:]))
        if (len(w) - 1) % len(BUCKETS): continue
        n = (len(w) - 1) // len(BUCKETS)
        ts.append(w[0]); vals.append([sum(w[1 + v * len(BUCKETS) + b] for v in range(n)) for b in range(len(BUCKETS))])
    return ts, vals, n


def stretches(ts, vals) -> list[tuple[int, int]]:
    """Busy stretches as (snapshot before, snapshot after): maximal runs of intervals not mostly halted."""
    out, start = [], None
    for i in range(1, len(ts)):
        dt = ts[i] - ts[i - 1]
        busy = dt > 0 and vals[i][HALTED] - vals[i - 1][HALTED] < IDLE * dt
        if busy and start is None: start = i - 1
        if not busy and start is not None: out.append((start, i - 1)); start = None
    if start is not None: out.append((start, len(ts) - 1))
    return out


def halted_rates(ts, vals) -> list[float]:
    """Each bucket's median rate (ns per ns) over the halted intervals: what a window collects while nothing runs."""
    per = [[] for _ in BUCKETS]
    for i in range(1, len(ts)):
        dt = ts[i] - ts[i - 1]
        if dt > 0 and vals[i][HALTED] - vals[i - 1][HALTED] >= IDLE * dt:
            for b in range(len(BUCKETS)): per[b].append((vals[i][b] - vals[i - 1][b]) / dt)
    return [statistics.median(p) if p else 0.0 for p in per]


def fits(p, a, b, ts, offset, step) -> bool:
    """Whether stretch (a, b) is phase p's under that TSC offset: it holds the phase and little more. It starts within
    a snapshot interval and 5 ms after the phase's TSC start, or up to EARLY_NS more before it (the host busy just
    before the phase: a console line), and ends no earlier than the phase's TSC end less 2.5 ms (an edge interval
    mostly halted is not in it) and no later than that plus three intervals, 10 ms (the line it prints, the sleep's
    entry) and 5% of the phase."""
    s = p['tsc0'] + offset; e = s + p['tsc_ns']
    return (s - step - EARLY_NS <= ts[a] <= s + step + 5e6
            and e - 2.5e6 <= ts[b] <= e + 0.05 * p['tsc_ns'] + 3 * step + 10e6)


def place_runs(runs, ts, segs) -> tuple[dict, dict]:
    """({(run, phase): stretch index}, what was tried) under the one TSC offset all phases share (natemu's TSC counts the
    host clock's nanoseconds from an offset fixed at the VM's start): the offset at which every phase has exactly one
    stretch that fits it, each stretch used once. Busy stretches no phase claims (the host writing a console line in a
    gap) are left alone. Empty when no offset, or more than one, places every phase."""
    if len(ts) < 2: return {}, dict(offsets=0)
    step = statistics.median(b - a for a, b in zip(ts, ts[1:]))
    phases = [p for r in sorted(runs) for p in runs[r]['phases']]

    def assign(offset):
        got = {}
        for p in phases:
            c = [k for k, (a, b) in enumerate(segs) if fits(p, a, b, ts, offset, step)]
            if len(c) != 1: return None
            got[(p['run'], p['phase'])] = c[0]
        return got if len(set(got.values())) == len(got) else None
    found, best = [], 0
    for r in sorted(runs):
        if not runs[r]['phases']: continue
        p0 = runs[r]['phases'][0]
        for a, _ in segs:
            got = assign(ts[a] - p0['tsc0'])
            if got is None: continue
            offset = statistics.median(ts[segs[k][0]] - p['tsc0'] for p in phases for k in [got[(p['run'], p['phase'])]])
            got = assign(offset) or got           # again at the median offset
            if got not in found: found.append(got)
    return (found[0] if len(found) == 1 else {}), dict(placements=len(found), stretches=len(segs), step_ns=step)


def fit(xs, ys) -> dict:
    """Least squares y = a + b x: slope, intercept, the slope's standard error (None with fewer than three points)."""
    n = len(xs); mx = sum(xs) / n; my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0: return dict(slope=None, intercept=my, se=None, n=n)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx; a = my - b * mx
    se = math.sqrt(sum((y - a - b * x) ** 2 for x, y in zip(xs, ys)) / (n - 2) / sxx) if n > 2 else None
    return dict(slope=b, intercept=a, se=se, n=n)


def summary(values) -> dict:
    v = [x for x in values if x is not None]
    if not v: return dict(n=0)
    return dict(n=len(v), median=statistics.median(v), min=min(v), max=max(v), stdev=statistics.stdev(v) if len(v) > 1 else 0.0)


def measure(runs, ts, vals) -> tuple[list[dict], list[str], dict]:
    """Per phase its buckets (ns per iteration) and x; the problems placing them; each run's placement."""
    segs, rates, rows, problems = stretches(ts, vals), halted_rates(ts, vals), [], []
    where, tried = place_runs(runs, ts, segs)
    placed = dict(tried, phases=len(where))
    if not where:
        return [], [f'the phases have no one place in the {len(segs)} busy stretches of the snapshots under one TSC offset '
                    f'({tried.get("placements", 0)} placements found)'], placed
    for r in sorted(runs):
        for p in runs[r]['phases']:
            k = where[(r, p['phase'])]
            a, b = segs[k]
            a, b = max(a - 1, 0), min(b + 1, len(ts) - 1)     # one halted interval more on each side: the whole phase
            outside = max(0, ts[b] - ts[a] - p['tsc_ns'])
            d = [vals[b][i] - vals[a][i] - rates[i] * outside for i in range(len(BUCKETS))]
            busy = sum(d) - d[HALTED]
            rows.append(dict(p, x=p['trips'] / p['iters'], guest=d[GUEST] / p['iters'], busy=busy / p['iters'],
                             buckets={n: d[i] / p['iters'] for i, n in enumerate(BUCKETS) if i != HALTED},
                             window_ns=ts[b] - ts[a]))
    return rows, problems, placed


def analyse(rows) -> dict:
    """The slopes per counted round and pooled, and the gate and cold-state costs from them."""
    counted = [r for r in rows if r['round'] > 0]
    rounds = sorted({(r['run'], r['round']) for r in counted})
    fps, variants = ('small', 'large'), ('trip', 'ctrl')

    def slope(sel, key):
        return fit([r['x'] for r in sel], [r[key] for r in sel]) if len(sel) >= 2 else dict(slope=None, se=None, n=len(sel))
    per_round = []
    for run, rnd in rounds:
        s = {}
        for fp in fps:
            for var in variants:
                sel = [r for r in counted if (r['run'], r['round'], r['fp'], r['variant']) == (run, rnd, fp, var)]
                s[f'{fp}_{var}'] = slope(sel, 'guest')['slope']; s[f'{fp}_{var}_busy'] = slope(sel, 'busy')['slope']
        net = {fp: None if None in (s[f'{fp}_trip'], s[f'{fp}_ctrl']) else s[f'{fp}_trip'] - s[f'{fp}_ctrl'] for fp in fps}
        whole = {fp: None if None in (s[f'{fp}_trip_busy'], s[f'{fp}_ctrl_busy']) else s[f'{fp}_trip_busy'] - s[f'{fp}_ctrl_busy']
                 for fp in fps}
        per_round.append(dict(run=run, round=rnd, slopes=s, gate_ns=net['small'],
                              cold_ns=None if None in net.values() else net['large'] - net['small'],
                              trip_small_ns=whole['small'], trip_large_ns=whole['large']))
    pooled = {f'{fp}_{var}': slope([r for r in counted if (r['fp'], r['variant']) == (fp, var)], 'guest')
              for fp in fps for var in variants}
    buckets = {}
    for fp in fps:
        for var in variants:
            sel = [r for r in counted if (r['fp'], r['variant']) == (fp, var)]
            if len(sel) >= 2:
                buckets[f'{fp}_{var}'] = {n: fit([r['x'] for r in sel], [r['buckets'][n] for r in sel])['slope'] for n in sel[0]['buckets']}
    return dict(rounds=per_round, pooled=pooled, bucket_slopes=buckets,
                gate_ns=summary([r['gate_ns'] for r in per_round]), cold_ns=summary([r['cold_ns'] for r in per_round]),
                trip_small_ns=summary([r['trip_small_ns'] for r in per_round]),
                trip_large_ns=summary([r['trip_large_ns'] for r in per_round]))


def judge(runs, record) -> list[str]:
    """The program's own results: complete runs, trip counts that match K, one work sum per footprint, the resident's
    CPUID count against the trips."""
    problems = []
    if record is not None and not record.get('verified'):
        problems.append('run.py: not verified (the image checks the program\'s sums against the ones pinned at build time)')
    if sorted(runs) != [1, 2]: problems.append(f'program runs {sorted(runs)}, not [1, 2]')
    sums, trips = {}, 0
    for r, d in sorted(runs.items()):
        want = int(d.get('begin', {}).get('phases', -1))
        if len(d['phases']) != want: problems.append(f'run {r}: {len(d["phases"])} phases, the program announced {want}')
        if d.get('end_ok') is not True: problems.append(f'run {r}: no end line with ok=1')
        if [p['phase'] for p in d['phases']] != list(range(len(d['phases']))): problems.append(f'run {r}: phases out of order')
        for p in d['phases']:
            exp = p['iters'] // p['k'] if p['k'] else 0
            if p['trips'] != exp: problems.append(f'run {r} phase {p["phase"]}: {p["trips"]} trips, K={p["k"]} gives {exp}')
            if sums.setdefault(p['fp'], p['sum']) != p['sum']: problems.append(f'run {r} phase {p["phase"]}: sum {p["sum"]}, not {sums[p["fp"]]}')
            if p['variant'] == 'trip': trips += p['trips']
    if record is not None:
        cpuid = int(((record.get('system_counts') or {}).get('kinds') or {}).get(CPUID_KIND, 0))
        if not trips <= cpuid <= trips + CPUID_SLACK:
            problems.append(f'the resident ran {cpuid} CPUIDs; the trip phases made {trips} (allowed up to {CPUID_SLACK} more)')
    return problems


def report(d: pathlib.Path) -> int:
    runs = parse_serial((d / f'{NAME}.serial').read_text(errors='replace'))
    ts, vals, n = parse_snaps((d / f'{NAME}.perf').read_text(errors='replace'))
    recs = [json.loads(l) for l in (d / 'bench.jsonl').read_text().splitlines() if l.strip()] if (d / 'bench.jsonl').exists() else []
    record = recs[-1] if recs else None
    problems = judge(runs, record)
    if record is None: problems.append('no run.py record (bench.jsonl)')
    if n != 1: problems.append(f'the performance log has {n} vCPUs in its snapshots, not 1 (no snapshots: a `perf` resident and --perf-log?)')
    rows, placing, placed = measure(runs, ts, vals) if n == 1 else ([], [], {})
    problems += placing
    result = dict(problems=problems, placed=placed, phases=rows, **(analyse(rows) if rows else {}),
                  cpuid=((record or {}).get('system_counts') or {}).get('kinds', {}).get(CPUID_KIND))
    (d / f'{NAME}.report.json').write_text(json.dumps(result, indent=1) + '\n')
    for what in ('gate_ns', 'cold_ns', 'trip_small_ns', 'trip_large_ns'):
        s = result.get(what, {})
        if s.get('n'):
            print(f'{what}: median {s["median"]:.1f} (min {s["min"]:.1f}, max {s["max"]:.1f}, stdev {s["stdev"]:.1f}; {s["n"]} rounds)')
    for k, v in result.get('pooled', {}).items():
        if v.get('slope') is not None: print(f'pooled guest slope {k}: {v["slope"]:.1f} ns/trip (se {v["se"] or 0:.1f})')
    print('\n'.join(f'PROBLEM: {p}' for p in problems) or 'judge: ok', f'\nreport: {d / (NAME + ".report.json")}')
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    for o in ('--out', '--kernel', '--initrd', '--fexc', '--natemu', '--runtime'): r.add_argument(o, required=True)
    r.add_argument('--append', default='', help='more kernel command-line words (passed to run.py)')
    r.add_argument('--cap', default='900')
    sub.add_parser('report').add_argument('dir')
    a = ap.parse_args()
    if a.cmd == 'report': return report(pathlib.Path(a.dir))
    cmd = [sys.executable, str(ROOT / 'tools/bench/run.py'), '--out', a.out, '--name', NAME, '--emu', 'natemu', '--vcpus', '1',
           '--bench', 'coldtrip', '--kernel', a.kernel, '--initrd', a.initrd, '--fexc', a.fexc, '--natemu', a.natemu,
           '--runtime', a.runtime, '--compile-cache', 'off', '--perf-log', '--cap', a.cap, *(['--append', a.append] if a.append else [])]
    print(' '.join(cmd), flush=True)
    rc = subprocess.run(cmd).returncode
    if rc not in (0, 1): return rc                # 3: contended
    return report(pathlib.Path(a.out)) or rc


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main())
