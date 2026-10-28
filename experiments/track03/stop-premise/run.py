#!/usr/bin/env python3
"""Build, sign and run the stop-premise matrix; write raw output to OUT/*.txt and print a markdown table.

    python3 -B experiments/track03/stop-premise/run.py --out /Users/liaozping/orca/natemu-raw/stop-premise-20261028-01 [--kicks 10000]
"""
import argparse, pathlib, re, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
ap = argparse.ArgumentParser(); ap.add_argument('--out', type=pathlib.Path, required=True)
ap.add_argument('--kicks', type=int, default=10000); ap.add_argument('--secs', type=int, default=5); ap.add_argument('--spinners', type=int, default=12)
a = ap.parse_args(); a.out.mkdir(parents=True, exist_ok=True)
subprocess.run([HERE / 'build.sh', a.out], check=True)
rows = []
for spin in (0, a.spinners):
    for mode in ('control', 'protect', 'exit'):
        for nv in (1, 2, 4):
            out = subprocess.run([a.out / 'probe', mode, str(nv), str(a.secs if mode == 'control' else a.kicks), str(spin)],
                                 capture_output=True, text=True).stdout
            (a.out / f'{mode}-{nv}v-{spin}spin.txt').write_text(out)
            g = lambda p: re.search(p, out)
            row = [mode, nv, spin]
            if mode == 'control': row += [f"{a.secs} s, exits=" + re.search(r'exits_while_running=(\d+)', out).group(1)] + ['-'] * 10
            else:
                d = lambda k: re.search(k + r'_ns n=\d+ min=(\d+) p50=(\d+) p99=(\d+) p99.9=(\d+) max=(\d+)', out).groups()
                m = re.search(r'kicks=(\d+) missed_1ms=(\d+) missed_100ms=(\d+) .*after_return=(\d+) missed_100ms_after_return=(\d+)', out).groups()
                row += [m[0], *m[1:3], *m[3:5], *d('latency'), *d('protect_call')[1:3]]
            rows.append(row)
print('| mode | vCPUs | spinners | kicks (control: exits) | missed 1 ms | missed 100 ms | missed 1 ms after call returned | missed 100 ms after return | latency min/p50/p99/p99.9/max (ns) | protect call p50/p99 (ns) |')
print('|' + '---|' * 10)
for r in rows:
    print('|', ' | '.join(map(str, r[:3])), '|', r[3], '|', ' | '.join(r[4:8]) if r[0] != 'control' else '- | - | - | -', '|',
          '/'.join(r[8:13]) if r[0] != 'control' else '-', '|', '/'.join(r[13:15]) if r[0] != 'control' else '-', '|')
