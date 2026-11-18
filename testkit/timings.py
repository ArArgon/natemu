"""Where a run's time went (`natemu-test times`, and the `times` section at the end of every run).

The controller writes <run>/timings.json: when the run started, when collection ended, every requirement prepared (and
whether it came from the cache or was built), and per unit when it asked for its claim, when it got it, when its
first test started (the worker's own start-up before that) and when it ended; runs.run adds the report and the cache
cleanup after pytest. Phases, in order:

    start + collection   process start, pytest, collecting family.toml files, selection
    plain tests          pytest items outside the run loop (the framework's own tests), if selected
    preparation          building or fetching artifacts, inputs and resources, and looking up the cached ones
    tests                first unit granted to last unit ended; units run in parallel, so this is wall time; the
                         part of it with no unit running is time spent waiting for other runs' claims
    report               pytest's report after the last unit
    cleanup              releasing the run's leases and cleaning the cache to its cap (after the summary is printed)

Per unit, its time splits into queue (asked to granted), start-up (worker process and collection) and tests.
"""
import collections
import json
import pathlib
import time

from testkit import paths


def path_of(rundir) -> pathlib.Path:
    return pathlib.Path(rundir) / 'timings.json'


def write(rundir, data: dict) -> None:
    f = path_of(rundir)
    tmp = f.with_suffix('.tmp'); tmp.write_text(json.dumps(data, indent=0)); tmp.replace(f)


def update(rundir, **kv) -> None:
    """Never raises: the times are a report, never a reason for a run to fail or skip its cleanup."""
    try:
        f = path_of(rundir)
        if not f.exists(): return
        d = json.loads(f.read_text()); d.update(kv); write(rundir, d)
    except Exception:
        pass


def fmt(s: float) -> str:
    s = max(0.0, s)
    if s < 60: return f'{s:.0f} s'
    m, s = divmod(round(s), 60)
    if m < 60: return f'{m} min {s:02d} s'
    h, m = divmod(m, 60)
    return f'{h} h {m:02d} min'


def _idle(units, lo, hi) -> float:
    """Seconds in [lo, hi] during which no unit ran."""
    spans = sorted((u['granted'], u['ended']) for u in units if u.get('granted') and u.get('ended'))
    idle, cur = 0.0, lo
    for a, b in spans:
        if a > cur: idle += a - cur
        cur = max(cur, b)
    return idle + max(0.0, hi - cur)


def phases(d: dict) -> list[tuple[str, float, str]]:
    """[(phase, seconds, note)] in order."""
    out = []
    t0, coll = d['started'], d.get('collected', d['started'])
    out.append(('start + collection', coll - t0, ''))
    if d.get('plain'):
        a, b = d['plain']; out.append(('plain tests', b - a, ''))
    p = d.get('prepare')
    if p:
        built = [r for r in p['reqs'] if r['built']]
        note = (f"{len(p['reqs'])} requirements: {len(built)} built or fetched ({fmt(sum(r['s'] for r in built))})"
                + (f", waited {fmt(p['waited'])} for a CPU claim" if p.get('waited', 0) >= 1 else ''))
        out.append(('preparation', p.get('end', p['start']) - p['start'], note))
    us = [u for u in d.get('units', []) if u.get('ended')]
    if us:
        a = min(u['granted'] for u in us); b = max(u['ended'] for u in us)
        busy = sum(u['ended'] - u['granted'] for u in us)
        idle = _idle(us, a, b)
        first_wait = a - (p.get('end', a) if p else coll)
        note = (f'{len(us)} units, {fmt(busy)} of unit time ({busy / max(b - a, 1e-9):.1f} at once on average)'
                + (f'; nothing running for {fmt(idle)}' if idle >= 1 else ''))
        if first_wait >= 1: out.append(('waiting for the first claim', first_wait, ''))
        out.append(('tests', b - a, note))
        end_tests = b
    else:
        end_tests = p.get('end', coll) if p else coll
    if d.get('pytest_end'): out.append(('report', d['pytest_end'] - end_tests, ''))
    if d.get('cleanup'):
        a, b = d['cleanup']; out.append(('cleanup', b - a, ''))
    return out


def by_family(d: dict) -> list[tuple]:
    g = collections.defaultdict(list)
    for u in d.get('units', []):
        if u.get('ended'): g[u['family']].append(u)
    rows = []
    for f, us in g.items():
        wall = max(u['ended'] for u in us) - min(u['granted'] for u in us)
        unit = sum(u['ended'] - u['granted'] for u in us)
        startup = sum((u['first_test'] or u['ended']) - u['granted'] for u in us)
        queue = sum(u['granted'] - u['asked'] for u in us)
        longest = max(us, key=lambda u: u['ended'] - u['granted'])
        rows.append((f, len(us), sum(u['items'] for u in us), wall, unit, startup, queue,
                     longest['label'], longest['ended'] - longest['granted']))
    return sorted(rows, key=lambda r: -r[4])


def brief(d: dict) -> list[str]:
    total = (d.get('finished') or d.get('pytest_end') or time.time()) - d['started']
    lines = [f'total {fmt(total)}']
    for name, s, note in phases(d):
        lines.append(f'  {name:28} {fmt(s):>12}' + (f'   {note}' if note else ''))
    rows = by_family(d)
    if rows:
        lines.append(f'  {"family":12} {"wall":>12} {"unit time":>12} {"units":>6} {"items":>7}   longest unit')
        for f, n, items, wall, unit, _, _, lab, ls in rows:
            lines.append(f'  {f:12} {fmt(wall):>12} {fmt(unit):>12} {n:6} {items:7}   {lab} ({fmt(ls)})')
    return lines


def detail(d: dict, top: int = 20) -> list[str]:
    lines = brief(d)
    rows = by_family(d)
    if rows:
        lines += ['', 'per family: unit time split into start-up (worker process and collection) and queue (asked to granted)']
        for f, n, items, wall, unit, startup, queue, _, _ in rows:
            lines.append(f'  {f:12} unit time {fmt(unit):>12}   start-up {fmt(startup):>10}   queue {fmt(queue):>12}')
    us = sorted((u for u in d.get('units', []) if u.get('ended')), key=lambda u: -(u['ended'] - u['granted']))
    if us:
        lines += ['', f'slowest {min(top, len(us))} units: queue / start-up / tests / total']
        for u in us[:top]:
            ft = u['first_test'] or u['ended']
            lines.append(f"  {u['label'][:52]:52} {u['core_class']}{u['cores']:<2} {fmt(u['granted'] - u['asked']):>12} "
                         f"{fmt(ft - u['granted']):>8} {fmt(u['ended'] - ft):>12} {fmt(u['ended'] - u['granted']):>12}")
    p = d.get('prepare')
    if p and p['reqs']:
        rs = sorted(p['reqs'], key=lambda r: -r['s'])
        lines += ['', f"preparation: {len(rs)} requirements, slowest first"]
        for r in rs[:top]:
            lines.append(f"  {r['kind']:12} {r['name'][:48]:48} {fmt(r['s']):>10}  "
                         f"{'built or fetched' if r['built'] else 'from the cache'}{'' if r['ok'] else ', failed'}")
    return lines


def main(args) -> int:
    det = '--detail' in args
    rest = [a for a in args if not a.startswith('--')]
    if rest:
        from testkit import runs
        d = runs._find(rest[0])
        if d is None: print(f'{rest[0]}: no such run'); return 2
    else:
        rd = paths.runs_dir()
        cand = sorted((r for r in rd.iterdir() if path_of(r).exists()), key=lambda r: path_of(r).stat().st_mtime) \
            if rd.exists() else []
        if not cand: print('no run with recorded times in this worktree'); return 2
        d = cand[-1]
    f = path_of(d)
    if not f.exists(): print(f'{d.name}: no times recorded (a run from before times were recorded?)'); return 2
    data = json.loads(f.read_text())
    print(f'run {d.name}')
    print('\n'.join(detail(data) if det else brief(data)))
    return 0
