#!/usr/bin/env python3
"""What sampling costs (the EL0 sampler's overhead case, docs/restructure/el0-sampler-plan.md chunk 0): bench boots
(tools/bench/run.py) of the resident built with its `perf` and `profile` features, sampling off and on (`--sample-us N`),
alternating (off then on in odd rounds, on then off in even ones), each configuration once per round, on this machine.

    python3 tests/bench/run_sampler_cost.py --out DIR --kernel BZIMAGE --initrd CPIO --fexc FEXC --natemu STUB --runtime RT
        [--sample-us 250] [--rounds 3] [--configs build:1,hackbench:1,build:4]

Both modes write the timing log (--perf-log), so they differ only in sampling. Per configuration and per run of the boot
(run 1 cold, run 2 warm), it compares the medians over rounds of the wall time (run.py's host-timed START to END) and of
the guest bucket's share of the vCPUs' busy time in that run (the resident's buckets from the timing log's 5 ms snapshots,
between the run's markers). Accepted when every wall time differs by no more than the rounds' spread (the larger of the two
modes' max - min) plus 2% of the off median, and every guest share by no more than 1 point. Writes DIR/result.json and
DIR/summary.md, and each sampling boot's report (tests/bench/sample_report.py: the measured samples per second). Exit 0
accepted, 1 not (or a boot failed), 3 a boot was contended."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import json
import statistics
import subprocess

import sample_report as sr

ROOT = pathlib.Path(__file__).resolve().parents[2]
WALL_SLACK = 0.02     # of the off median, beyond the rounds' spread
SHARE_POINTS = 1.0


def boot(a, d, bench, vcpus, sample):
    """One run.py boot into folder d: (exit status, its bench.jsonl record or None)."""
    cmd = [sys.executable, str(ROOT / 'tools/bench/run.py'), '--out', str(d), '--name', bench, '--emu', 'natemu', '--vcpus', str(vcpus),
           '--bench', bench, '--kernel', a.kernel, '--initrd', a.initrd, '--fexc', a.fexc, '--natemu', a.natemu, '--runtime', a.runtime,
           '--compile-cache', 'off', '--perf-log', *(['--sample-us', str(a.sample_us)] if sample else [])]
    print(' '.join(cmd), flush=True)
    code = subprocess.run(cmd, cwd=ROOT).returncode
    f = d / 'bench.jsonl'
    rec = json.loads(f.read_text().splitlines()[-1]) if f.exists() and f.read_text().strip() else None
    return code, rec


def guest_shares(d, bench, names):
    """{run1, run2: the guest bucket's share of busy time in that run, in %}, from the timing log and the markers."""
    perf, markers = d / f'{bench}.perf', d / f'{bench}.markers.tsv'
    if not perf.exists() or not markers.exists(): return {}
    snap = sr.snaps(perf)
    out = {}
    for n, t0, t1 in sr.marker_windows(sr.read_markers(markers), 0):
        if n in ('run1', 'run2'):
            b = sr.buckets_between(snap, t0, t1, names)
            busy = b and sum(b.values()) - b.get('halted', 0)
            if busy: out[n] = round(100 * b.get('guest', 0) / busy, 2)
    return out


def compare(rows):
    """Per (config, run): wall and guest-share verdicts."""
    out, ok = {}, True
    for cfg in dict.fromkeys(r['config'] for r in rows):
        for run, wall in (('run1', 'wall_s'), ('run2', 'wall2_s')):
            vals = {m: [r[wall] for r in rows if r['config'] == cfg and r['mode'] == m] for m in ('off', 'on')}
            shares = {m: [r['guest_share'].get(run) for r in rows if r['config'] == cfg and r['mode'] == m] for m in ('off', 'on')}
            e = dict(wall_off=vals['off'], wall_on=vals['on'], share_off=shares['off'], share_on=shares['on'])
            if any(v is None for m in vals for v in vals[m]) or not all(vals.values()) or len(vals['off']) != len(vals['on']):
                e.update(accepted=False, why='a wall time is missing')
            elif any(v is None for m in shares for v in shares[m]):
                e.update(accepted=False, why='a guest share is missing')
            else:
                mo, mn = statistics.median(vals['off']), statistics.median(vals['on'])
                spread = max(max(v) - min(v) for v in vals.values())
                bound = spread + WALL_SLACK * mo
                so, sn = statistics.median(shares['off']), statistics.median(shares['on'])
                e.update(wall_median_off=mo, wall_median_on=mn, wall_diff=round(mn - mo, 3), wall_diff_pct=round(100 * (mn - mo) / mo, 2),
                         spread=round(spread, 3), wall_bound=round(bound, 3), share_median_off=so, share_median_on=sn,
                         share_diff=round(sn - so, 2))
                why = [f'wall differs by {abs(mn - mo):.3f} s > {bound:.3f} s (spread + 2%)'] if abs(mn - mo) > bound else []
                why += [f'guest share differs by {abs(sn - so):.2f} points > {SHARE_POINTS}'] if abs(sn - so) > SHARE_POINTS else []
                e.update(accepted=not why, why='; '.join(why))
            ok &= e['accepted']
            out[f'{cfg}/{run}'] = e
    return ok, out


def summary(res):
    lines = [f'# Sampling cost: every {res["sample_us"]} us, {res["rounds"]} rounds, alternating', '',
             '| config/run | wall off (s) | wall on (s) | diff % | spread (s) | bound (s) | guest % off | guest % on | diff (points) | rate on (/s per vCPU) | verdict |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |']
    for k, e in res['compare'].items():
        cfg = k.split('/')[0]
        rates = [x for r in res['boots'] if r['config'] == cfg and r['mode'] == 'on' for x in r.get('samples_per_s', [])]
        rate = f'{statistics.median(rates):.0f}' if rates else ''
        lines.append(f'| {k} | {e.get("wall_median_off", "")} | {e.get("wall_median_on", "")} | {e.get("wall_diff_pct", "")} | '
                     f'{e.get("spread", "")} | {e.get("wall_bound", "")} | {e.get("share_median_off", "")} | {e.get("share_median_on", "")} | '
                     f'{e.get("share_diff", "")} | {rate} | {"accepted" if e["accepted"] else "NOT accepted: " + e["why"]} |')
    lines += ['', 'Medians over the rounds; a profile build (perf and profile features) in both modes.', '']
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    for n in ('--out', '--kernel', '--initrd', '--fexc', '--natemu', '--runtime'): ap.add_argument(n, required=True)
    ap.add_argument('--sample-us', type=int, default=250)
    ap.add_argument('--rounds', type=int, default=3)
    ap.add_argument('--configs', default='build:1,hackbench:1,build:4')
    a = ap.parse_args()
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
    configs = [(c.split(':')[0], int(c.split(':')[1])) for c in a.configs.split(',')]
    names = sr.bucket_names()
    rows, failed, contended = [], [], False
    for k in range(1, a.rounds + 1):
        for bench, vcpus in configs:
            for mode in (('off', 'on') if k % 2 else ('on', 'off')):
                cfg = f'{bench}-{vcpus}'
                d = out / f'{cfg}-{mode}-r{k}'
                code, rec = boot(a, d, bench, vcpus, mode == 'on')
                contended |= code == 3
                if code not in (0, 3) or rec is None or not rec.get('verified'):
                    failed.append(f'{d.name}: exit {code}')
                row = dict(config=cfg, mode=mode, round=k, exit=code, wall_s=rec and rec.get('wall_s'), wall2_s=rec and rec.get('wall2_s'),
                           guest_share=guest_shares(d, bench, names))
                if mode == 'on':
                    problems = sr.report_one(d / bench, d / f'{bench}.markers.tsv', d / f'{bench}.perf')
                    if problems: failed += [f'{d.name}: {p}' for p in problems]
                    rep = d / f'{bench}.report.json'
                    if rep.exists():
                        row['samples_per_s'] = list(json.loads(rep.read_text())['phases'][0]['samples_per_s'].values())
                rows.append(row)
                print(json.dumps(row), flush=True)
    ok, cmp = compare(rows)
    res = dict(sample_us=a.sample_us, rounds=a.rounds, configs=a.configs, boots=rows, compare=cmp, failed=failed,
               contended=contended, accepted=ok and not failed)
    (out / 'result.json').write_text(json.dumps(res, indent=1) + '\n')
    (out / 'summary.md').write_text(summary(res))
    print(summary(res))
    for f in failed: print('failed:', f)
    return 3 if contended else 0 if res['accepted'] else 1


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main())
