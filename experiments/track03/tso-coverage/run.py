#!/usr/bin/env python3
"""Build, sign and run the TSO coverage probe; write raw results and a summary.

    python3 -B experiments/track03/tso-coverage/run.py --out /Users/liaozping/orca/natemu-raw/tso-coverage-20261027-01 [--iters 10000000]

Each cell runs in its own fresh process/VM (a fault in one cell cannot hide others).  Litmus cells run as two timing
profiles of iters/2 each (random post-barrier delay up to 2^6 / 2^12 spins, different seeds).  TSO-off controls run the
core litmus cells under the same plan.  Raw rows: OUT/{on,off}/<cell>.jsonl; verdict inputs: OUT/results.json.
"""
import argparse, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import oracle  # noqa: E402
INPUTS = ('probe.c', 'guest.S', 'gen.py', 'litmus.py', 'oracle.py', 'run.py', 'README.md', 'entitlements.plist')
CORE = ('mp', 'lb', 'wrc', 'iriw', 'sb')
PROFILES = ((63, 1), (4095, 2))


def sh(cmd, **kw):
    r = subprocess.run([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, **kw)
    if r.returncode: raise RuntimeError(' '.join(map(str, cmd)) + '\n' + r.stdout)
    return r.stdout


def build(out):
    b = out / 'build'; b.mkdir(parents=True, exist_ok=True)
    src = out / 'source'; src.mkdir(exist_ok=True)
    for n in INPUTS:
        if (HERE / n).exists(): shutil.copyfile(HERE / n, src / n)
    sh([sys.executable, '-B', src / 'gen.py', b], cwd=src)
    shutil.copyfile(src / 'guest.S', b / 'guest.S')
    sh(['xcrun', 'clang', '-arch', 'arm64', '-c', b / 'cells.S', '-o', b / 'cells.o'])
    sdk = sh(['xcrun', '--show-sdk-path']).strip()
    sh(['xcrun', 'clang', '-arch', 'arm64', '-isysroot', sdk, '-std=c11', '-Wall', '-Wextra', '-Werror', '-O2', '-pthread',
        src / 'probe.c', b / 'cells.o', '-framework', 'Hypervisor', '-o', b / 'probe'])
    sh(['codesign', '--force', '--sign', '-', '--entitlements', src / 'entitlements.plist', b / 'probe'])
    cells = json.load(open(b / 'cells.json'))
    # mnemonic audit of the generated bodies (which memory instructions each kind of cell really uses)
    dis = sh(['xcrun', 'otool', '-tvV', b / 'cells.o'])
    (out / 'disasm-sample.txt').write_text(''.join(extract(dis, cells)))
    (out / 'mnemonics.json').write_text(json.dumps(audit(dis, cells), indent=1))
    return b, cells


def audit(dis, cells):
    """memory-ordering-relevant mnemonics used by the generated bodies, per kind of cell (litmus rows exclude the harness)"""
    mn, cur, kind = {}, None, ()
    for line in dis.splitlines():
        m = re.match(r'^_c(\d+)_(t\d|fin):', line)
        if m:
            cur = cells[int(m.group(1))]
            kind = (cur['litmus'] if cur['kind'] == 'lit' else 'storm', cur.get('wmode', '') if cur['kind'] == 'atom' else '', 'handler-capable' if cur['ex'] else '')
        elif re.match(r'^\S+:', line): cur = None
        elif cur is not None:
            f = line.split('\t')
            if len(f) > 1 and re.match(r'^(ld|st|swp|cas|dmb|dsb|isb|svc|eret|hvc|prf|dc)', f[1].strip()):
                mn.setdefault(' '.join(k for k in kind if k), set()).add(f[1].strip())
    return {k: sorted(v) for k, v in mn.items()}


def extract(dis, cells):
    want = {c['name'] for c in cells if c['name'] in ('mp.w8-al.r', 'mp.w4-u16.u', 'ex-mp-b.w8-u16.r', 'atom.w8-u16.r.swpal', 'atom.w4-al.r.split', 'mix.st8-ld4lohi.u16')}
    ids = {c['id'] for c in cells if c['name'] in want}
    keep, cur = [], False
    for line in dis.splitlines(True):
        m = re.match(r'^_c(\d+)_(t\d|fin):', line)
        if m: cur = int(m.group(1)) in ids
        elif re.match(r'^\S+:', line): cur = False
        if cur: keep.append(line)
    return keep


def run_cell(probe, mode, cell, iters, outdir, scale):
    plan = outdir / (cell['name'] + '.plan')
    if cell['kind'] == 'atom': lines = [(0, 0, iters)]
    else: lines = [(m, s, iters // len(PROFILES)) for m, s in PROFILES]
    plan.write_text(''.join(f"{cell['id']} {cell['name']} {n} {mask} {seed + cell['id'] * 17}\n" for mask, seed, n in lines))
    try:
        r = subprocess.run([str(probe), mode, str(plan)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                           timeout=300 + int(iters * 3e-5 * scale))
        rc, so, se = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        rc, so, se = -9, (e.stdout or b'').decode() if isinstance(e.stdout, bytes) else (e.stdout or ''), 'timeout'
    (outdir / (cell['name'] + '.jsonl')).write_text(so + ('' if not se else json.dumps({'stderr': se}) + '\n'))
    plan.unlink()
    rows = [json.loads(l) for l in so.splitlines() if l.startswith('{')]
    return rc, [r for r in rows if r['kind'] == 'cell' and r.get('ok')], rows, se


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True, type=Path)
    ap.add_argument('--iters', type=int, default=10_000_000)
    ap.add_argument('--atom-iters', type=int, default=10_000_000)
    ap.add_argument('--only', help='substring filter on cell names')
    ap.add_argument('--no-off', action='store_true')
    a = ap.parse_args()
    out = a.out; out.mkdir(parents=True, exist_ok=True)
    probe_dir, cells = build(out)
    probe = probe_dir / 'probe'
    (out / 'host.txt').write_text(''.join(sh(c) for c in (['sw_vers'], ['uname', '-a'], ['sysctl', 'hw.model', 'hw.ncpu', 'hw.cachelinesize', 'hw.perflevel0.logicalcpu'])))
    sh([sys.executable, '-B', HERE / 'oracle.py', '--self-check'])
    results = []
    t0 = time.time()
    def go(mode, cell, iters):
        d = out / mode; d.mkdir(exist_ok=True)
        rc, rows, allrows, se = run_cell(probe, mode, cell, iters, d, 1)
        want = 1 if cell['kind'] == 'atom' else len(PROFILES)
        if rc != 0 or len(rows) != want:
            res = dict(name=cell['name'], litmus=cell['litmus'], cls=cell['cls'], width=cell['width'], mode=mode, failed=True,
                       rc=rc, stderr=se.strip()[-300:], status=[r.get('status') for r in allrows if r['kind'] == 'cell'], iters=0)
        else:
            res = oracle.check_cell(cell, rows); res['mode'] = mode
        res['id'] = cell['id']; res['kind'] = cell['kind']; res['insn'] = cell['insn']; res['wmode'] = cell.get('wmode', '')
        results.append(res)
        print(f"[{time.time() - t0:6.0f}s] {mode:3} {cell['name']:32} " + ('FAILED rc=%s %s' % (rc, res.get('stderr')) if res.get('failed') else
              ' '.join(f'{k}={res[k]}' for k in ('trials', 'forbidden', 'torn', 'weak', 'reads', 'transitions') if k in res)), file=sys.stderr, flush=True)
    sel = [c for c in cells if not a.only or a.only in c['name']]
    for c in sel: go('on', c, a.atom_iters if c['kind'] == 'atom' else a.iters)
    if not a.no_off:
        for c in sel:
            if c['kind'] == 'lit' and c['litmus'] in CORE and not c['ex'] and c['cls'] in ('al', 'u16'): go('off', c, a.iters)
    (out / 'results.json').write_text(json.dumps(dict(iters=a.iters, atom_iters=a.atom_iters, profiles=PROFILES, results=results), indent=0))
    summarize(results, out)


def fault_of(r):
    """'FAULT esr=..' for a cell whose guest took an exception, else 'FAILED'."""
    try:
        st = [x for x in r['status'] if x][0]
        return 'FAULT(esr=%s far=%s)' % (st[0][1], st[0][3] if len(st[0]) > 3 else '?')
    except Exception:
        return 'FAILED(rc=%s)' % r.get('rc')


def summarize(results, out):
    L = []
    cell = lambda r: ('FAULT' if r.get('failed') else None)
    def sel(**kw):
        return [r for r in results if all((r.get(k) in v if isinstance(v, tuple) else r.get(k) == v) for k, v in kw.items())]
    ordering = lambda r: r['kind'] == 'lit' and r['litmus'] not in ('mix', 'tear', 'tear-swp', 'tear-split')
    L.append('## per-form table, EnTSO on (ordering = all litmus programs of the form; tear = 1 writer + 3 readers re-fetching a fresh line)')
    L.append(f"{'form':10} {'ord cells':>9} {'ord trials':>14} {'FORBIDDEN':>9} {'tear rounds':>13} {'TORN':>9} {'swpal rounds':>13} {'swpal torn':>10}   storm reads/transitions/torn   swpal-storm")
    for w in (1, 2, 4, 8):
        for cls in ('al', 'u16', 'x16', 'x64', 'x128', 'pg'):
            o = [r for r in results if r['mode'] == 'on' and r['width'] == w and r['cls'] == cls and ordering(r) and not r.get('failed')]
            if not o and not sel(mode='on', width=w, cls=cls): continue
            t = [r for r in sel(mode='on', width=w, cls=cls, litmus='tear') if not r.get('failed')]
            sw = sel(mode='on', width=w, cls=cls, litmus='tear-swp')
            st = [r for r in sel(mode='on', width=w, cls=cls, kind='atom') if r['name'].endswith('.r') or r['name'].endswith('.u') or cls == 'pg' and 'swpal' not in r['name'] and 'split' not in r['name']]
            ss = sel(mode='on', width=w, cls=cls, kind='atom', wmode='swpal') + [r for r in sel(mode='on', width=w, cls=cls, kind='atom') if r['name'].endswith('.swpal')]
            def storm(rs):
                rs = [r for r in rs if not r.get('failed')]
                return '-' if not rs else '%d/%d/%d' % (sum(r['reads'] for r in rs), sum(r['transitions'] for r in rs), sum(r['torn'] for r in rs))
            sws = '-' if not sw else (fault_of(sw[0]) if sw[0].get('failed') else str(sw[0]['trials']))
            swt = '-' if not sw else ('-' if sw[0].get('failed') else str(sw[0]['torn']))
            L.append(f"w{w}-{cls:5}  {len(o):9} {sum(r['trials'] for r in o):14} {sum(r['forbidden'] for r in o):9} {sum(r['trials'] for r in t):13} {sum(r['torn'] for r in t):9} {sws:>13} {swt:>10}   {storm(st)}   {storm(ss) if not any(r.get('failed') for r in ss) else fault_of([r for r in ss if r.get('failed')][0])}")
    L.append('## per-litmus (EnTSO on): litmus / class -> cells, trials, forbidden, torn (digit 3), TSO-only outcomes (e.g. store buffering), min trials per cell, failed cells')
    for lit in sorted({r['litmus'] for r in results if r['mode'] == 'on' and r['kind'] == 'lit'}):
        for cls in ('al', 'u16', 'x16', 'x64', 'x128', 'pg'):
            g = [r for r in sel(mode='on', litmus=lit, cls=cls)]
            if not g: continue
            ok = [r for r in g if not r.get('failed')]
            L.append(f"{lit:11} {cls:5} cells={len(g):3} trials={sum(r['trials'] for r in ok):>13} forbidden={sum(r['forbidden'] for r in ok):>9} torn={sum(r['torn'] for r in ok):>10} weak={sum(r['weak'] for r in ok):>10} min={min([r['trials'] for r in ok] or [0])} failed={len(g) - len(ok)}")
    L.append('## TSO-off controls (same cells, same plan): forbidden outcomes per litmus')
    for lit in CORE:
        g = [r for r in sel(mode='off', litmus=lit) if not r.get('failed')]
        if g: L.append(f"{lit:5} cells={len(g):3} trials={sum(r['trials'] for r in g):>13} forbidden={sum(r['forbidden'] for r in g):>7} weak={sum(r['weak'] for r in g):>9}  per-cell forbidden: " + ' '.join(f"{r['name']}:{r['forbidden']}" for r in g if r['forbidden']))
    mp_lb = sum(r['forbidden'] for r in sel(mode='off') if r['litmus'] in ('mp', 'lb'))
    L.append(f"control verdict: {'CONCLUSIVE (MP/LB violations seen with EnTSO off: %d)' % mp_lb if mp_lb else 'INCONCLUSIVE (no MP/LB violation with EnTSO off)'}")
    L.append('## failed cells (guest exception or probe failure)')
    for r in results:
        if r.get('failed'): L.append(f"{r['mode']:3} {r['name']:34} {fault_of(r)}")
    L.append('## storm atomicity cells (EnTSO on): writer stores, reads, reader value transitions, torn reads')
    for r in results:
        if r['kind'] == 'atom' and r['mode'] == 'on' and not r.get('failed'):
            L.append(f"{r['name']:34} stores={r['writer_stores']} reads={r['reads']} transitions={r['transitions']} torn={r['torn']} first={r['first_torn']}")
    (out / 'summary.txt').write_text('\n'.join(L) + '\n')
    print('\n'.join(L))


if __name__ == '__main__': main()
