"""Replay recorded runs through the scheduling rules (testkit.sched.policy.decide), without running anything: the way to
check a scheduling change (`tools/natemu-test replay`).

Every unit in the runs' timings.json (testkit.timings) becomes a request at its recorded ask time, with its recorded
length (granted to ended) as both its duration and its expected duration, its cores, and exclusive when it ran
exclusively. The machine is this one unless --cores P,E is given. The report gives each group's delay from the other
runs (turnaround, first ask to last end, minus the run's turnaround alone on the machine): the small runs (at most 10
units), all interactive runs, the gate runs; and the longest unit wait.
"""
import argparse
import glob
import json
import math
import pathlib
import statistics

from testkit.sched import policy
from testkit.sched.policy import Config, Grant, Request, State, decide


def load(roots) -> list[dict]:
    """Recorded runs under each root's .natemu/runs (and its .claude/worktrees/*/.natemu/runs)."""
    runs = []
    for root in roots:
        root = pathlib.Path(root)
        for f in sorted(glob.glob(str(root / '.natemu/runs/*/timings.json')) +
                        glob.glob(str(root / '.claude/worktrees/*/.natemu/runs/*/timings.json'))):
            t = json.loads(pathlib.Path(f).read_text())
            try: args = json.loads((pathlib.Path(f).parent / 'run.json').read_text())['args']
            except (OSError, ValueError, KeyError): args = []
            us = [u for u in t.get('units', []) if u.get('granted') and u.get('ended')]
            if not us: continue
            runs.append({'id': pathlib.Path(f).parent.name, 'gate': '--gate' in args, 'units': [
                {'ask': u['asked'], 'dur': max(0.1, u['ended'] - u['granted']),
                 'cores': 1 if u['core_class'] == 'X' else u['cores'], 'excl': u['core_class'] == 'X',
                 'cls': u['core_class']} for u in us]})
    return runs


def simulate(runs: list[dict], cfg: Config) -> dict:
    """run id -> {'turnaround', 'units', 'gate', 'waits'} under the scheduling rules, event by event."""
    reqs, unit = [], {}
    slow = getattr(cfg, 'e_slowdown', 1.0)      # a unit recorded on an efficiency core took that many times as long
    for r in runs:
        for i, u in enumerate(r['units']):
            dur = u['dur'] / slow if u.get('cls') == 'E' else u['dur']
            q = Request(id=f"{r['id']}#{i}", owner_pid=0, cores=u['cores'], exclusive=u['excl'],
                        priority=policy.GATE if r['gate'] else policy.INTERACTIVE, asked=u['ask'], expected=dur)
            if 'run' in Request.__dataclass_fields__: q.run = r['id']     # fair share (rule 7), where the rules have it
            reqs.append(q); unit[q.id] = (r['id'], dur)
    reqs.sort(key=lambda q: q.asked)
    st, pending, running, done = State(), list(reqs), {}, {}
    now = reqs[0].asked if reqs else 0.0
    while pending or st.waiting or running:
        while pending and pending[0].asked <= now: st.waiting.append(pending.pop(0))
        d = decide(st, cfg, now)
        st.window_open_since, st.last_window_closed = d.window_open_since, d.last_window_closed
        for q, cls in d.grants:
            st.waiting.remove(q)
            g = Grant(q, now, cls); st.held.append(g); running[q.id] = (g, now + unit[q.id][1] * (slow if cls == 'E' else 1.0))
            if hasattr(policy, 'granted'): policy.granted(st, q, now)
        if d.grants: continue
        nxt = min([e for _, e in running.values()] + ([pending[0].asked] if pending else []), default=None)
        now = now + 1 if nxt is None else max(nxt, now)          # nothing to wait for: a timing rule, step on
        for qid, (g, e) in list(running.items()):
            if e > now: continue
            st.held.remove(g); del running[qid]; done[qid] = (g.start, e)
            if hasattr(st, 'served'):
                k = policy.run_of(g.request); st.served[k] = st.served.get(k, 0.0) + (e - g.start) * policy.cost(g, cfg)
    out = {}
    for q in reqs:
        rid, _ = unit[q.id]; s, e = done[q.id]
        o = out.setdefault(rid, {'ask': math.inf, 'end': 0.0, 'units': 0, 'gate': q.priority == policy.GATE, 'waits': []})
        o['ask'] = min(o['ask'], q.asked); o['end'] = max(o['end'], e); o['units'] += 1; o['waits'].append(s - q.asked)
    for o in out.values(): o['turnaround'] = o['end'] - o['ask']
    return out


def delays(runs: list[dict], cfg: Config, out: dict) -> None:
    """Each run's delay: its turnaround minus its turnaround alone on the machine (what the others cost it)."""
    for r in runs:
        alone = simulate([dict(r, units=[dict(u) for u in r['units']])], cfg)[r['id']]['turnaround']
        out[r['id']]['delay'] = max(0.0, out[r['id']]['turnaround'] - alone)


def summary(out: dict) -> dict:
    p90 = lambda xs: sorted(xs)[max(0, math.ceil(0.9 * len(xs)) - 1)] if xs else 0.0
    sel = {'small': [o for o in out.values() if not o['gate'] and o['units'] <= 10],
           'interactive': [o for o in out.values() if not o['gate']], 'gate': [o for o in out.values() if o['gate']]}
    res = {k: {'runs': len(v), 'mean': statistics.mean(o['delay'] for o in v) if v else 0.0,
               'p90': p90([o['delay'] for o in v]), 'max': max((o['delay'] for o in v), default=0.0)}
           for k, v in sel.items()}
    res['worst_wait'] = max((w for o in out.values() for w in o['waits']), default=0.0)
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('roots', nargs='*', default=['.'], help='checkouts whose recorded runs to replay (default: this one)')
    ap.add_argument('--cores', help='P,E (default: this machine)')
    a = ap.parse_args(argv)
    from testkit.sched.pool import default_config
    cfg = default_config()
    if a.cores:
        p, e = map(int, a.cores.split(',')); cfg = Config(p_cores=p, e_cores=e, window_budget=cfg.window_budget,
                                                           min_shared=cfg.min_shared, e_slowdown=cfg.e_slowdown)
    runs = load(a.roots)
    if not runs: print('no recorded runs (timings.json) to replay'); return 2
    out = simulate(runs, cfg); delays(runs, cfg, out)
    s = summary(out)
    print(f'{len(runs)} runs, {sum(len(r["units"]) for r in runs)} units, {cfg.p_cores} P + {cfg.e_cores} E cores')
    for k in ('small', 'interactive', 'gate'):
        v = s[k]
        print(f'  {k:12} {v["runs"]:4} runs   delay from others: mean {v["mean"]:7.0f} s   p90 {v["p90"]:7.0f} s   max {v["max"]:7.0f} s')
    print(f'  longest unit wait {s["worst_wait"]:.0f} s')
    return 0
