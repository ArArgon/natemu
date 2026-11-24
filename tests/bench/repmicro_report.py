#!/usr/bin/env python3
"""REP MOVS copies in the guest (docs/restructure/bulk-rep-plan.md section 8, the bulk REP plan's kill-test; bench case
guest-repmicro).

    repmicro_report.py run --out DIR --kernel K --initrd CPIO --fexc F --natemu N --runtime R [--append WORDS]
    repmicro_report.py report DIR
    repmicro_report.py compare --before DIR... --after DIR...

`run` boots the bench image's repmicro programs (tools/bench/run.py --bench repmicro, 1 vCPU: the 64-bit build's rows,
then the -m32 build's, twice in the boot), then reports. `report` reads DIR/repmicro.serial (the programs'
NATEMU-REPMICRO lines: per row ns per copy, the median, least and most of three rounds of about 50 ms) and
DIR/bench.jsonl (run.py's record: the image's check of every row's destination checksum against a byte-by-byte copy's,
pinned at build time), and writes DIR/repmicro.report.json. The judge: both runs verified and every row in both, with
the checksum of the pinned set. A row's value is run 2's median (run 1 translates the code first).

`compare` is the kill-test over the reports of runs before and after a change (three each, the plan says): a hot row's
result is the median across the runs; any other row (short, declined, 32-bit) regresses if its median after is above the
largest before by more than the before spread (largest less least). Killed when `movsb-4096` is not 4x faster, or
`movsq-4096` not 1.2x, or any other row regresses. It prints the table and exits 0 when not killed, 1 when killed."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse, json, re, statistics, subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
NAME = 'repmicro'
LINE = re.compile(r'NATEMU-REPMICRO run=(\d+) bits=(\d+) row=(\S+) bytes=(\d+) iters=(\d+) ns_med=([\d.]+) ns_min=([\d.]+) '
                  r'ns_max=([\d.]+) sum=([0-9a-f]+)')
HOT = ('movsb-16', 'movsb-64', 'movsb-128', 'movsb-512', 'movsb-4096', 'movsb-65536',
       'movsq-64', 'movsq-100', 'movsq-128', 'movsq-512', 'movsq-4096')
ROWS64 = HOT + ('movsb-1', 'movsb-3', 'movsb-7', 'movsb-15', 'movsq-8', 'movsq-4096-src4', 'movsb-4096-overlap8',
                'movsq-4096-src808')
ROWS32 = ('movsd-4', 'movsd-8', 'movsd-12', 'movsd-4096', 'movsd-4096-off4')
KILL = {'movsb-4096': 4.0, 'movsq-4096': 1.2}       # the least speedup that keeps the change


def parse_serial(text: str) -> dict:
    """{run: {row: {...}}} from the programs' lines."""
    runs = {}
    for line in text.replace('\r', '').splitlines():
        m = LINE.search(line)
        if not m: continue
        run, bits, row, nbytes, iters, med, lo, hi, s = m.groups()
        runs.setdefault(int(run), {})[row] = dict(bits=int(bits), bytes=int(nbytes), iters=int(iters), ns_med=float(med),
                                                  ns_min=float(lo), ns_max=float(hi), sum=s)
    return runs


def report(d: pathlib.Path) -> int:
    serial = d / f'{NAME}.serial'
    runs = parse_serial(serial.read_text(errors='replace')) if serial.exists() else {}
    jl = d / 'bench.jsonl'
    record = json.loads([l for l in jl.read_text().splitlines() if l.strip()][-1]) if jl.exists() else None
    problems = []
    if record is None: problems.append('no run.py record (bench.jsonl)')
    elif not record.get('verified'):
        problems.append('run.py: not verified (the image checks every row\'s checksum against a byte-by-byte copy\'s)')
    for run in (1, 2):
        missing = [r for r in ROWS64 + ROWS32 if r not in runs.get(run, {})]
        if missing: problems.append(f'run {run}: rows missing: {", ".join(missing)}')
    sums = {r: {runs[k][r]['sum'] for k in runs if r in runs[k]} for r in ROWS64 + ROWS32}
    for r, s in sums.items():
        if len(s) > 1: problems.append(f'{r}: checksums differ between runs {sorted(s)}')
    rows = {r: runs.get(2, {}).get(r, {}).get('ns_med') for r in ROWS64 + ROWS32}
    out = dict(case='guest-repmicro', rows=rows, runs=runs, problems=problems, verified=bool(record and record.get('verified')))
    (d / f'{NAME}.report.json').write_text(json.dumps(out, indent=1) + '\n')
    for r in ROWS64 + ROWS32: print(f'{r:24} {rows[r] if rows[r] is not None else "-":>12} ns')
    print('\n'.join(f'PROBLEM: {p}' for p in problems) or 'judge: ok', f'\nreport: {d / (NAME + ".report.json")}')
    return 1 if problems else 0


def compare(before: list[dict], after: list[dict]) -> tuple[bool, list[dict]]:
    """The kill-test: (killed, a row per row) from report JSONs' `rows`."""
    out, killed = [], False
    for r in ROWS64 + ROWS32:
        b = [x['rows'][r] for x in before if x['rows'].get(r) is not None]
        a = [x['rows'][r] for x in after if x['rows'].get(r) is not None]
        if not b or not a:
            out.append(dict(row=r, verdict='missing')); killed = True; continue
        mb, ma = statistics.median(b), statistics.median(a)
        row = dict(row=r, before_median=mb, before_min=min(b), before_max=max(b), after_median=ma, after_min=min(a),
                   after_max=max(a), speedup=mb / ma if ma else None)
        if r in KILL:
            row['verdict'] = 'ok' if row['speedup'] >= KILL[r] else f'killed: {row["speedup"]:.2f}x < {KILL[r]}x'
        elif r in HOT: row['verdict'] = 'hot'
        else:
            bound = max(b) + (max(b) - min(b))
            row['bound'] = bound
            row['verdict'] = 'ok' if ma <= bound else f'regressed: {ma:.2f} > {bound:.2f}'
        killed |= row['verdict'].startswith(('killed', 'regressed'))
        out.append(row)
    return killed, out


def compare_main(a) -> int:
    load = lambda ds: [json.loads((pathlib.Path(d) / f'{NAME}.report.json').read_text()) for d in ds]
    killed, rows = compare(load(a.before), load(a.after))
    f = lambda x: '' if x is None else f'{x:.2f}'
    print('| row | before median (min-max), ns | after median (min-max), ns | speedup | verdict |')
    print('| --- | ---: | ---: | ---: | --- |')
    for r in rows:
        if r['verdict'] == 'missing': print(f'| {r["row"]} | | | | missing |'); continue
        print(f'| {r["row"]} | {f(r["before_median"])} ({f(r["before_min"])}-{f(r["before_max"])}) | {f(r["after_median"])} '
              f'({f(r["after_min"])}-{f(r["after_max"])}) | {f(r["speedup"])}x | {r["verdict"]} |')
    print('KILLED' if killed else 'kept')
    return 1 if killed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    for o in ('--out', '--kernel', '--initrd', '--fexc', '--natemu', '--runtime'): r.add_argument(o, required=True)
    r.add_argument('--append', default='', help='more kernel command-line words (passed to run.py)')
    r.add_argument('--cap', default='900')
    sub.add_parser('report').add_argument('dir')
    c = sub.add_parser('compare')
    c.add_argument('--before', nargs='+', required=True); c.add_argument('--after', nargs='+', required=True)
    a = ap.parse_args()
    if a.cmd == 'report': return report(pathlib.Path(a.dir))
    if a.cmd == 'compare': return compare_main(a)
    cmd = [sys.executable, str(ROOT / 'tools/bench/run.py'), '--out', a.out, '--name', NAME, '--emu', 'natemu', '--vcpus', '1',
           '--bench', 'repmicro', '--kernel', a.kernel, '--initrd', a.initrd, '--fexc', a.fexc, '--natemu', a.natemu,
           '--runtime', a.runtime, '--compile-cache', 'off', '--cap', a.cap, *(['--append', a.append] if a.append else [])]
    print(' '.join(cmd), flush=True)
    rc = subprocess.run(cmd).returncode
    if rc not in (0, 1): return rc                # 3: contended
    return report(pathlib.Path(a.out)) or rc


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main())
