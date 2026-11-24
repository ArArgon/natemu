#!/usr/bin/env python3
"""The computed-exit census reduced to the decision numbers (docs/restructure/el0-sampler-plan.md, chunk 2 and section
7), from the census plugin's JSON (tests/bench/plugins/indirect.c) of one segment (a benchmark's run 1, an io5 step):

- indirect jmp and call executions at sites with one target and with two (and three, four, five or more), user, kernel
  and total; each also with the sites keyed by their physical code instead of their address space and address (natemu
  shares translated code between processes by frame, so an inline cache in shared code sees every process's targets);
- ret executions, and the return-stack model's hit rate at depths 8, 16 and 32 (a pop of an empty stack is a miss);
- each per 1,000 guest instructions executed (time under TCG means nothing here).

    python3 tests/bench/census_report.py CENSUS.json [--window run1] [--json OUT.json]

run_census.py calls reduce() and markdown() for each case; run as a program it refuses outside the framework."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import json

MODES = ('user', 'kernel')
CLASSES = ('1', '2', '3', '4', '5+')
DEPTHS = ('8', '16', '32')
COLS = ('kind', 'mode', 'asid', 'pc', 'phys', 'flags', 'execs', 'overflow', 'targets')


def sites(seg: dict) -> list[dict]:
    return [dict(zip(COLS, s)) for s in seg['sites']]


def distinct(site: dict) -> str:
    """How many targets the site had: '1' to '4', or '5+' (the plugin keeps 4 and counts the rest as overflow)."""
    return '5+' if site['overflow'] or len(site['targets']) > 4 else str(len(site['targets']))


def regroup(ss: list[dict], key) -> list[dict]:
    """Sites merged by `key` (their targets' counts summed): a merged site has the union of its parts' targets, and 5+
    if any part had 5 or more."""
    out = {}
    for s in ss:
        k = key(s)
        m = out.setdefault(k, dict(s, execs=0, overflow=0, targets={}))
        m['execs'] += s['execs']; m['overflow'] += s['overflow']
        for t, c in s['targets']: m['targets'][t] = m['targets'].get(t, 0) + c
    for m in out.values(): m['targets'] = sorted(m['targets'].items())
    return list(out.values())


def by_targets(ss: list[dict]) -> dict:
    execs = sum(s['execs'] for s in ss)
    cls = {c: sum(s['execs'] for s in ss if distinct(s) == c) for c in CLASSES}
    return {'execs': execs, 'sites': len(ss), 'execs_by_targets': cls,
            'sites_by_targets': {c: sum(1 for s in ss if distinct(s) == c) for c in CLASSES},
            'share_1': cls['1'] / execs if execs else None, 'share_2': cls['2'] / execs if execs else None,
            'share_le2': (cls['1'] + cls['2']) / execs if execs else None}


def physical(s: dict) -> tuple:
    return (s['kind'], s['mode'], s['phys'] or s['pc'])


def merge_segments(segs: list[dict], name: str) -> dict:
    """Segments summed into one (counts added; sites merged by kind, address space and address)."""
    out = {'name': name, 'insns': {m: 0 for m in MODES}, 'direct_calls': {m: 0 for m in MODES},
           'ras': {d: {m: {'hit': 0, 'miss': 0, 'empty': 0} for m in MODES} for d in DEPTHS},
           'events': {}, 'far': {k: {m: 0 for m in MODES} for k in ('call', 'jmp', 'ret')}}
    for s in segs:
        for m in MODES:
            out['insns'][m] += s['insns'][m]; out['direct_calls'][m] += s['direct_calls'][m]
            for k in out['far']: out['far'][k][m] += s['far'][k][m]
            for d in DEPTHS:
                for r in ('hit', 'miss', 'empty'): out['ras'][d][m][r] += s['ras'][d][m][r]
        for k, v in s['events'].items(): out['events'][k] = out['events'].get(k, 0) + v
    merged = regroup([x for s in segs for x in sites(s)], lambda x: (x['kind'], x['mode'], x['asid'], x['pc']))
    out['sites'] = [[m[c] for c in COLS] for m in merged]
    return out


def reduce(data: dict, window: str) -> dict:
    """The decision numbers of segment `window` ('all': every segment summed)."""
    segs = data['segments']
    if window == 'all': seg = merge_segments(segs, 'all')
    else:
        seg = next((s for s in segs if s['name'] == window), None)
        if seg is None: raise ValueError(f'no segment {window!r} (segments {[s["name"] for s in segs]})')
    ss = sites(seg)
    r = {'window': window, 'cr3': data.get('cr3'), 'rip': data.get('rip'), 'vcpus': data.get('vcpus'),
         'events': seg['events'], 'modes': {}}
    for mode in (*MODES, 'total'):
        mine = [s for s in ss if mode == 'total' or s['mode'] == mode]
        ind = [s for s in mine if s['kind'] in ('call', 'jmp')]
        insns = sum(seg['insns'].values()) if mode == 'total' else seg['insns'][mode]
        ras = {d: {k: sum(seg['ras'][d][m][k] for m in MODES if mode in ('total', m)) for k in ('hit', 'miss', 'empty')}
               for d in DEPTHS}
        calls = sum(s['execs'] for s in ind if s['kind'] == 'call')
        jmps = sum(s['execs'] for s in ind if s['kind'] == 'jmp')
        rets = sum(s['execs'] for s in mine if s['kind'] == 'ret')
        direct = sum(seg['direct_calls'][m] for m in MODES if mode in ('total', m))
        per = lambda n: 1000 * n / insns if insns else None
        r['modes'][mode] = {
            'insns': insns,
            'indirect': by_targets(ind), 'indirect_call': by_targets([s for s in ind if s['kind'] == 'call']),
            'indirect_jmp': by_targets([s for s in ind if s['kind'] == 'jmp']),
            'indirect_by_code': by_targets(regroup(ind, physical)),
            'ret': by_targets([s for s in mine if s['kind'] == 'ret']),
            'ras': {d: {**v, 'hit_rate': v['hit'] / sum(v.values()) if sum(v.values()) else None} for d, v in ras.items()},
            'per_1000': {'indirect_call': per(calls), 'indirect_jmp': per(jmps), 'indirect': per(calls + jmps),
                         'ret': per(rets), 'direct_call': per(direct)},
        }
    return r


def _pct(x): return '' if x is None else f'{100 * x:.1f}%'
def _num(x): return '' if x is None else f'{x:.2f}'


def markdown(r: dict, name: str) -> str:
    m = r['modes']
    rows = [('guest instructions', lambda v: f'{v["insns"]:,}'),
            ('indirect jmp + call per 1,000 instructions', lambda v: _num(v['per_1000']['indirect'])),
            ('  of which calls', lambda v: _num(v['per_1000']['indirect_call'])),
            ('indirect executions at 1-target sites', lambda v: _pct(v['indirect']['share_1'])),
            ('indirect executions at 2-target sites', lambda v: _pct(v['indirect']['share_2'])),
            ('indirect executions at 1-target sites, by physical code', lambda v: _pct(v['indirect_by_code']['share_1'])),
            ('indirect executions at 2-target sites, by physical code', lambda v: _pct(v['indirect_by_code']['share_2'])),
            ('indirect sites (1 / 2 / 3 / 4 / 5+ targets)', lambda v: ' / '.join(str(v['indirect']['sites_by_targets'][c]) for c in CLASSES)),
            ('ret per 1,000 instructions', lambda v: _num(v['per_1000']['ret'])),
            ('direct call per 1,000 instructions', lambda v: _num(v['per_1000']['direct_call']))]
    rows += [(f'return model hit rate, depth {d}', lambda v, d=d: _pct(v['ras'][d]['hit_rate'])) for d in DEPTHS]
    out = [f'### Census {name}, segment {r["window"]}', '', '| | user | kernel | total |', '| --- | ---: | ---: | ---: |']
    out += [f'| {label} | ' + ' | '.join(f(m[k]) for k in ('user', 'kernel', 'total')) + ' |' for label, f in rows]
    out += ['', f'CR3 {r["cr3"]}; RIP {r["rip"]}; events {r["events"]}.', '']
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('census'); ap.add_argument('--window', default='run1'); ap.add_argument('--json')
    a = ap.parse_args()
    r = reduce(json.loads(pathlib.Path(a.census).read_text()), a.window)
    if a.json: pathlib.Path(a.json).write_text(json.dumps(r, indent=1) + '\n')
    print(markdown(r, pathlib.Path(a.census).stem))


if __name__ == '__main__':
    testenv.require_framework(__file__)
    main()
