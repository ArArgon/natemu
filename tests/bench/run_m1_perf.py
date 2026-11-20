#!/usr/bin/env python3
"""M1 performance check driver (docs/m1-config-and-qcow2-design.md, "Performance check"): calls the existing fio matrix
(tests/io/run_io_matrix.py) and IO5 (tests/io/run_io5.py) runners one after another, records every run, and judges the result.

    run_m1_perf.py plan   [--final-dir D]      what `full` would run, counts, estimated minutes, exact runner command lines
    run_m1_perf.py smoke  [--final-dir D]      one matrix run on raw and one on preallocated qcow2 (plumbing + analysis)
    run_m1_perf.py full   [--final-dir D]      the whole comparison; resumable (DIR/runs.jsonl); no per-run images left behind
    run_m1_perf.py analyze DIR                 rerun the analysis over DIR/runs.jsonl -> DIR/summary.md, DIR/summary.json
    run_m1_perf.py self-check                  the analysis math on synthetic data

Inputs (plan, smoke, full) default to natemu-raw (RAW_ROOT); each can be given instead, as tools/natemu-test's bench case
`m1-perf` does with builds and inputs it made itself: --baseline-build/--final-build DIR (a natemu build laid out like
cargo's target folder; old names --baseline-dir/--final-dir), --fexc, --fio-prep, --block-inputs (what the matrix inputs
are prepared from, into --inputs), --io5-inputs. Under a caller that already holds the VM lock (NATEMU_VM_LOCK_HELD) the
driver does not take it again.

Sets (full --sets, default A,BC,D):
  A   baseline raw vs final raw, 5 runs each, strictly interleaved ABBA (A B B A A B B A A B)
  BC  final raw vs final qcow2 (preallocated) vs final overlay (empty overlay on the raw base), 3 runs each, interleaved
      (the raw side is its own fresh set of runs in this session, not reused from A)
  D   IO5 (4 vCPUs, --solo-build, --mitigations-off) once raw and once overlay with the final binary; compared on solo-build make
The matrix is 2 vCPUs on the PCI transport, the same command line for every setup (only --image-format differs).

Judging. Per job: median/min/max of the runs, noise = (max-min)/median (the largest over the raw setups), ratio of medians in the
direction of performance (throughput higher is better; latency and time lower is better). A loss no larger than the noise is
'noise' and counts as 1.0. Overall = geometric mean of the counted per-job ratios of the throughput jobs (and IO5's solo make).
PASS: loss under 10%; AIM: under 5%; a job over 5% loss is FLAGged, over 20% hard-FLAGged (also when inside its noise). Any
failed guest verification, e2fsck, digest or `qemu-img check` in a run (the runners fail the run) is an automatic FAIL.
Overlay comparisons (C, D) measure cluster allocation: reported, not gated.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import time

IO = testenv.TESTS / 'io'          # the io family's scripts it runs
from io_common import VM_LOCK  # noqa: E402
from raw_root import RAW_ROOT  # noqa: E402

PERF = RAW_ROOT / 'm1-perf'
BASELINE_COMMIT = 'ecec5d77'   # main after the Nov 17 rebase (was 33a99fb8)
FEXC = RAW_ROOT / 'batch1-fexc-20261116-03/obj/fexc'
FIO_PREP = RAW_ROOT / 'io-benchmark-prep-20261108-01'
BLOCK_INPUTS = RAW_ROOT / 'io3-block-20261108-01/inputs-01'
IO5_INPUTS = RAW_ROOT / 'io5-inputs-20261113-01'
PROFILES = ('precondition', 'seq-read-4k-q16', 'seq-read-128k-q16', 'seq-write-128k-q16', 'rand-read-4k-q1', 'rand-read-4k-q16',
            'rand-read-4k-q64', 'rand-read-4k-4jobs-q16', 'mixed-4k-q16', 'fsync-4k-q1', 'verify-all')
# stonewalled profiles run their four jobs one after another: the rate is bytes over summed time, not the sum of rates
SERIAL = ('precondition', 'verify-all')
# setup -> (binary set, image format, comparison role)
SETUPS = {'base-raw': ('baseline', 'raw'), 'final-raw': ('final', 'raw'), 'final-raw-bc': ('final', 'raw'),
          'final-qcow2': ('final', 'qcow2'), 'final-overlay': ('final', 'overlay'),
          'io5-raw': ('final', 'raw'), 'io5-overlay': ('final', 'overlay')}
RAW_SETUPS = ('base-raw', 'final-raw', 'final-raw-bc')
# (id, baseline setup, new setup, gated, what a loss would come from)
COMPARISONS = (
    ('A', 'base-raw', 'final-raw', True, 'seam cost: the extra indirection per request, per-request allocation, lock or barrier on the raw path'),
    ('B', 'final-raw-bc', 'final-qcow2', True, 'qcow2 table lookup/L2 cache, table lock, allocation of metadata or the extra copy in the request path'),
    ('C', 'final-raw-bc', 'final-overlay', False, 'cluster allocation and backing-file reads on first write (precondition), table lock'),
    ('D', 'io5-raw', 'io5-overlay', False, 'cluster allocation while the build writes, table lock, backing reads'),
)
MATRIX_MIN, IO5_MIN = 3.0, 15.0   # earlier natemu 2-vCPU runs: ~2 min (n2-01: 94+28 s) plus digests; IO5 (branch-solo): ~13 min


# ---------------------------------------------------------------- plan
def build_plan(sets):
    plan = []
    if 'A' in sets:
        for rep in range(5):
            for s in (('base-raw', 'final-raw') if rep % 2 == 0 else ('final-raw', 'base-raw')): plan.append(('A', s, rep))
    if 'BC' in sets:
        orders = (('final-raw-bc', 'final-qcow2', 'final-overlay'), ('final-overlay', 'final-qcow2', 'final-raw-bc'),
                  ('final-qcow2', 'final-raw-bc', 'final-overlay'))
        for rep, order in enumerate(orders):
            for s in order: plan.append(('BC', s, rep))
    if 'D' in sets:
        for s in ('io5-raw', 'io5-overlay'): plan.append(('D', s, 0))
    return [dict(id=f'{g}-{s}-{r}', group=g, setup=s, rep=r, kind='io5' if s.startswith('io5') else 'matrix') for g, s, r in plan]


def paths(args, setup):
    d = Path(args.baseline_dir if SETUPS[setup][0] == 'baseline' else args.final_dir)
    return d / 'release/natemu', d / 'aarch64-unknown-none/release/natemu-runtime'


def command(args, spec, out, manifest='MANIFEST'):
    binary, runtime = paths(args, spec['setup'])
    fmt = SETUPS[spec['setup']][1]
    flags = ['--image-format', fmt] if fmt != 'raw' else []   # raw: no flag, so baseline and final lines are identical
    if spec['kind'] == 'matrix':
        return [sys.executable, IO / 'run_io_matrix.py', 'run', '--backend', 'natemu', '--transport', 'pci', '--cpus', '2',
                '--executable', binary, '--runtime', runtime, '--fexc', args.fexc, '--inputs', args.inputs, '--out', out,
                '--lock', args.lock, '--build-manifest', manifest] + flags
    return [sys.executable, IO / 'run_io5.py', 'run', '--backend', 'natemu', '--inputs', args.io5_inputs, '--out', out,
            '--natemu', binary, '--runtime', runtime, '--fexc', args.fexc, '--vcpus', '4', '--ram', '8', '--mitigations-off',
            '--solo-build', '--lock', args.lock] + flags


def show(cmd): return ' '.join(str(c) for c in cmd)


INCOMPLETE = 4      # `full --limit N`: this invocation's runs were ok and runs are left (testkit runs the next piece)


def plan(args):
    p = build_plan(args.sets.split(','))
    if args.count: print(len(p)); return 0
    m, i = sum(s['kind'] == 'matrix' for s in p), sum(s['kind'] == 'io5' for s in p)
    print(f'{len(p)} runs: {m} matrix (2 vCPUs, PCI) + {i} IO5; estimated {m * MATRIX_MIN + i * IO5_MIN:.0f} minutes '
          f'({MATRIX_MIN:.0f} min/matrix run, {IO5_MIN:.0f} min/IO5 run) plus a minute of preparation')
    print('Matrix inputs prepared once (if missing):', show([sys.executable, IO / 'run_io_matrix.py', 'prepare', '--fio-prep', args.fio_prep,
          '--block-inputs', args.block_inputs, '--out', args.inputs, '--transport', 'pci']))
    print('Set A reuses nothing; set BC runs its own fresh raw side (3 raw + 3 qcow2 + 3 overlay, rotated order).')
    for n, s in enumerate(p, 1): print(f'{n:2d} {s["id"]:<28} {show(command(args, s, Path(args.dir or "DIR") / "runs" / s["id"]))}')
    print('Order of set A:', ' '.join('A' if s['setup'] == 'base-raw' else 'B' for s in p if s['group'] == 'A'), '(A = baseline, B = final)')


# ---------------------------------------------------------------- machine state, runs
def free_disk(p):
    """Free bytes on the volume of `p` (its nearest existing folder: the results folder may not exist yet)."""
    p = Path(p).absolute()
    while not p.exists(): p = p.parent
    return shutil.disk_usage(p).free


@contextlib.contextmanager
def vm_lock(lock):
    """The VM lock for one run, exclusively (a blocking wait behind other runners), unless the caller already holds it
    (NATEMU_VM_LOCK_HELD: natemu-test holds it for the whole case; taking it again would wait forever)."""
    if os.environ.get('NATEMU_VM_LOCK_HELD'):
        yield; return
    print('  waiting for the VM lock', flush=True)
    with Path(lock).open('a') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def sh_out(*c):
    r = subprocess.run([str(x) for x in c], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def others():
    """VM processes (natemu, qemu) running now that are not descendants of this driver."""
    rows = [l.split(None, 2) for l in sh_out('ps', '-axo', 'pid=,ppid=,command=').splitlines() if len(l.split(None, 2)) == 3]
    mine = {os.getpid()}
    for _ in range(4): mine |= {int(p) for p, pp, _ in rows if int(pp) in mine}
    return sorted(c[:140] for p, _, c in rows if int(p) not in mine and re.search(r'(/natemu|/natemu-host|qemu-system-x86_64)$', c.split()[0]))


def env_record(args, dest):
    rec = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'pgrep_natemu': [l[:150] for l in sh_out('pgrep', '-fl', 'natemu').splitlines()], 'other_vm_processes': others(),
           'loadavg': os.getloadavg(), 'free_disk_bytes': free_disk(dest.parent), 'repo_commit': sh_out('git', '-C', testenv.ROOT, 'rev-parse', 'HEAD'),
           'baseline_commit': BASELINE_COMMIT, 'final_commit': args.final_commit or sh_out('git', '-C', testenv.ROOT, 'rev-parse', 'HEAD'),
           'sha256': {}, 'fexc': str(args.fexc), 'matrix_input_image_sha256': None}
    for k, d in (('baseline', args.baseline_dir), ('final', args.final_dir)):
        for rel in ('release/natemu', 'aarch64-unknown-none/release/natemu-runtime'):
            if (Path(d) / rel).exists(): rec['sha256'][f'{k}/{rel}'] = sha(Path(d) / rel)
    if Path(args.fexc).exists(): rec['sha256']['fexc'] = sha(args.fexc)
    pin = Path(args.inputs) / 'inputs.json'
    if pin.exists(): rec['matrix_input_image_sha256'] = json.loads(pin.read_text())['outputs']['root.raw']
    dest.write_text(json.dumps(rec, indent=1))
    return rec


def ensure_inputs(args):
    if (Path(args.inputs) / 'inputs.json').exists(): return
    if Path(args.inputs).exists():           # an interrupted preparation (inputs.json is written last): start it again
        print('removing an unfinished matrix-input preparation:', args.inputs, flush=True)
        shutil.rmtree(args.inputs)
    print('preparing matrix inputs (once):', args.inputs, flush=True)
    subprocess.run([sys.executable, IO / 'run_io_matrix.py', 'prepare', '--fio-prep', args.fio_prep, '--block-inputs', args.block_inputs,
                    '--out', args.inputs, '--transport', 'pci'], check=True)


def manifest(args, spec, dest):
    binary, runtime = paths(args, spec['setup'])
    f = dest / f'build-{SETUPS[spec["setup"]][0]}.json'
    arts = {str(p.resolve()): sha(p) for p in (binary, runtime, Path(args.fexc))}
    f.write_text(json.dumps({'artifacts': arts}))
    return f


def metrics_matrix(out):
    res = json.loads((out / 'results.json').read_text())
    if len(res) != 2 or not all(r['passed'] for r in res): return None, [e for r in res for e in r['errors']] or ['incomplete']
    fio, m = res[0]['fio'], {}
    for prof in PROFILES:
        jobs = fio[prof]
        ios = [(d, j[d]) for j in jobs for d in ('read', 'write')]
        if prof in SERIAL:
            m[prof + '/bw'] = sum(q['io_bytes'] for _, q in ios) / (sum(q['runtime'] for _, q in ios if q['io_bytes']) / 1000)
        else: m[prof + '/bw'] = sum(q['bw_bytes'] for _, q in ios)
        w = [(q['total_ios'], q['clat_ns']['mean']) for _, q in ios if q['io_bytes']]
        m[prof + '/lat'] = sum(n * l for n, l in w) / sum(n for n, _ in w)
        if prof == 'fsync-4k-q1': m[prof + '/fsynclat'] = jobs[0]['sync']['lat_ns']['mean']
    return m, []


def metrics_io5(out):
    r = json.loads((out / 'result.json').read_text())
    problems = [f'{p}: {x}' for p, v in r['phases'].items() for x in v['problems']] or ([] if r['pass'] else ['not passed'])
    if problems: return None, problems
    st = r['phases']['use']['steps']
    return {f'io5/{k}': float(st[k][1]) for k in ('solo-make', 'solo-configure', 'make', 'configure') if k in st}, []


def clean(out):
    for pat in ('*.raw', '*.qcow2', 'compile-cache*', 'serve'):
        for p in out.glob(pat): shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)


def one_run(args, spec, dest):
    out = dest / 'runs' / spec['id']
    logf = dest / 'logs' / (spec['id'] + '.log')
    if free_disk(dest) < (4 if spec['kind'] == 'io5' else 3) << 30:
        raise SystemExit('stopping: less than 3-4 GiB free disk (rerun after freeing space; finished runs are kept)')
    mf = manifest(args, spec, dest)
    # The driver holds the real VM lock for the whole run (a blocking wait in the queue of the other agents' runners); the runner
    # itself gets a private lock file, because the matrix runner would otherwise fail at once when anyone else is queued.
    cmd = command(argparse.Namespace(**dict(vars(args), lock=dest / 'runner.lock')), spec, out, mf)
    with vm_lock(args.lock):
        before = others()
        began = time.time()
        if out.exists(): shutil.rmtree(out)
        with logf.open('w') as lf:
            rc = subprocess.run([str(c) for c in cmd], stdout=lf, stderr=subprocess.STDOUT, timeout=3 * 3600 if spec['kind'] == 'io5' else 1800).returncode
        after = others()
    try: m, problems = (metrics_matrix if spec['kind'] == 'matrix' else metrics_io5)(out)
    except (OSError, KeyError, ValueError, IndexError) as e: m, problems = None, [f'no usable result: {e!r} (rc {rc})']
    clean(out)
    return dict(spec, ok=m is not None and rc == 0, problems=problems, rc=rc, seconds=round(time.time() - began), finished=time.time(), metrics=m or {},
                others_before=before, others_after=after, dir=str(out), command=show(cmd), time=time.strftime('%F %T'))


def load_runs(dest):
    f = Path(dest) / 'runs.jsonl'
    latest = {}
    for line in (f.read_text().splitlines() if f.exists() else []):
        r = json.loads(line); latest[r['id']] = r
    return latest


def execute(args, dest, plan_):
    dest = Path(dest); (dest / 'logs').mkdir(parents=True, exist_ok=True); (dest / 'runs').mkdir(exist_ok=True)
    ensure_inputs(args)
    env_record(args, dest / 'env.json')
    done = load_runs(dest)
    limit, ran, failed = getattr(args, 'limit', None), 0, False
    before = getattr(args, 'retry_failed_before', None)
    # a failed record is run again with --retry-failed, or with --retry-failed-before T if it was recorded before T
    # (a test-framework session's start: what failed in this session is not retried by each later piece)
    retry = lambda r: not r['ok'] and (args.retry_failed or (before is not None and r.get('finished', 0) < before))
    for n, spec in enumerate(plan_, 1):
        old = done.get(spec['id'])
        if old and not retry(old):
            print(f'[{n}/{len(plan_)}] {spec["id"]}: already recorded ({"ok" if old["ok"] else "FAILED"}), skipped', flush=True); continue
        if limit is not None and ran >= limit:      # --limit: the rest is left for the next invocation (a test-framework piece)
            left = sum(1 for s in plan_[n - 1:] if not done.get(s['id']) or retry(done[s['id']]))
            print(f'{left} runs left for later invocations (--limit {limit})', flush=True)
            return 1 if failed else INCOMPLETE
        print(f'[{n}/{len(plan_)}] {spec["id"]}: starting {time.strftime("%T")}', flush=True)
        r = one_run(args, spec, dest); ran += 1; failed |= not r['ok']; done[spec['id']] = r
        with (dest / 'runs.jsonl').open('a') as f: f.write(json.dumps(r) + '\n')
        extra = f' CONTENDED by {r["others_before"] + r["others_after"]}' if r['others_before'] or r['others_after'] else ''
        print(f'[{n}/{len(plan_)}] {spec["id"]}: {"ok" if r["ok"] else "FAILED " + str(r["problems"])} in {r["seconds"]} s{extra}', flush=True)
    return analyze(dest)


# ---------------------------------------------------------------- analysis
def direction(job): return 'lower' if job.endswith(('/lat', '/fsynclat')) or job.startswith('io5/') else 'higher'
def gated(job): return job.endswith('/bw') or job == 'io5/solo-make'
def spread(xs): return (max(xs) - min(xs)) / statistics.median(xs) if len(xs) > 1 else None


def compare(base, new, noise):
    """base/new: lists of metric dicts; noise: job -> spread or None. Rows per job plus the geometric means."""
    rows, counted, raw = [], [], []
    for job in sorted(set(base[0]) & set(new[0])) if base and new else []:
        b, n = [m[job] for m in base if job in m], [m[job] for m in new if job in m]
        mb, mn = statistics.median(b), statistics.median(n)
        perf = mn / mb if direction(job) == 'higher' else mb / mn
        loss, nz = 1 - perf, noise.get(job)
        within = nz is not None and abs(loss) <= nz   # no measured noise (single runs): every difference counts
        verdict = 'noise' if within else ('FLAG>20%' if loss > .20 else 'FLAG>5%' if loss > .05 else 'ok')
        if loss > .20 and within: verdict = 'FLAG>20% (inside noise)'
        row = dict(job=job, dir=direction(job), base_median=mb, base_min=min(b), base_max=max(b), new_median=mn, new_min=min(n), new_max=max(n),
                   n_base=len(b), n_new=len(n), ratio=perf, noise=nz, verdict=verdict, gated=gated(job))
        rows.append(row)
        if gated(job): counted.append(1.0 if within else perf); raw.append(perf)
    geo = lambda xs: math.exp(sum(map(math.log, xs)) / len(xs)) if xs else None
    return rows, geo(counted), geo(raw)


def judge(ok_fail, geo_counted, is_gated):
    if ok_fail: return 'FAIL (a run failed verification/checks)'
    if geo_counted is None: return 'n/a'
    loss = 1 - geo_counted
    v = 'FAIL (loss >= 10%)' if loss >= .10 else 'PASS (aim met, loss < 5%)' if loss < .05 else 'PASS (loss 5-10%, over the aim)'
    return v if is_gated else 'REPORT ONLY, not gated: ' + v


def analysis(runs):
    """runs: {id: run record}. Returns the summary dict."""
    by = {}
    for r in runs.values(): by.setdefault(r['setup'], []).append(r)
    for rs in by.values(): rs.sort(key=lambda r: r['id'])
    good = lambda s: [r['metrics'] for r in by.get(s, []) if r['ok']]
    noise = {}
    for s in RAW_SETUPS:
        ms = good(s)
        for job in (ms[0] if ms else {}):
            sp = spread([m[job] for m in ms if job in m])
            if sp is not None: noise[job] = max(noise.get(job, 0), sp)
    out = {'noise': noise, 'comparisons': {}, 'failed_runs': [], 'contended_runs': []}
    for r in runs.values():
        if not r['ok']: out['failed_runs'].append({'id': r['id'], 'problems': r['problems']})
        if r['others_before'] or r['others_after']: out['contended_runs'].append({'id': r['id'], 'others': r['others_before'] + r['others_after']})
    for cid, b, n, is_gated, cause in COMPARISONS:
        if not by.get(b) or not by.get(n): continue
        rows, geo, raw = compare(good(b), good(n), {} if cid == 'D' else noise)
        failed = any(not r['ok'] for r in by[b] + by[n])
        out['comparisons'][cid] = dict(base=b, new=n, gated=is_gated, rows=rows, geomean_counted=geo, geomean_raw=raw, cause_candidates=cause,
                                       verdict=judge(failed, geo, is_gated), n_runs={b: len(by[b]), n: len(by[n])},
                                       flags5=[r['job'] for r in rows if r['verdict'].startswith('FLAG')],
                                       flags20=[r['job'] for r in rows if r['verdict'].startswith('FLAG>20')])
    out['verdict'] = 'FAIL' if out['failed_runs'] else ('FAIL' if any(c['verdict'].startswith('FAIL') for c in out['comparisons'].values()) else 'PASS')
    return out


def pct(x): return 'n/a' if x is None else f'{x:.1%}'


def fmt(x): return '-' if x is None else (f'{x:,.0f}' if abs(x) >= 1000 else f'{x:.3g}')


def markdown(s):
    L = ['# M1 performance check', '', f'Overall: **{s["verdict"]}** (gated comparisons A and B; any failed run is an automatic FAIL; overlay C and D are reported only).', '']
    if s['failed_runs']: L += ['## Failed runs', ''] + [f'- {f["id"]}: {f["problems"]}' for f in s['failed_runs']] + ['']
    if s['contended_runs']:
        L += ['## WARNING: other VM processes were running during these runs (noise)', ''] + [f'- {c["id"]}: {c["others"]}' for c in s['contended_runs']] + ['']
    for cid, c in s['comparisons'].items():
        L += [f'## {cid}: {c["base"]} -> {c["new"]}  ({c["n_runs"][c["base"]]} vs {c["n_runs"][c["new"]]} runs)', '',
              f'Verdict: **{c["verdict"]}**. Geometric mean of performance ratios (noise counted as 1.0): '
              f'{fmt(c["geomean_counted"])}; uncorrected: {fmt(c["geomean_raw"])}.']
        if c['flags5']: L += [f'FLAGGED jobs (loss over 5%): {", ".join(c["flags5"])}. Cause candidates (notes, not findings): {c["cause_candidates"]}.']
        if c['flags20']: L += [f'HARD FLAG (over 20% slower): {", ".join(c["flags20"])}.']
        L += ['', '| job | better | base median (min..max) | new median (min..max) | ratio | noise | verdict |', '|---|---|---|---|---|---|---|']
        for r in c['rows']:
            L.append(f'| {r["job"]}{"" if r["gated"] else " (info)"} | {r["dir"]} | {fmt(r["base_median"])} ({fmt(r["base_min"])}..{fmt(r["base_max"])}) | '
                     f'{fmt(r["new_median"])} ({fmt(r["new_min"])}..{fmt(r["new_max"])}) | {r["ratio"]:.3f} | {pct(r["noise"])} | {r["verdict"]} |')
        L.append('')
    L += ['Ratio = performance of the new setup over the base setup (above 1 is better; latency and time are inverted). Noise = (max-min)/median of the raw '
          'setups\' runs; a loss inside it counts as no loss. Unmarked jobs are throughput (bw, bytes/s) and gate the geometric mean; "(info)" jobs are '
          'latencies (ns) or IO5 sub-times (s). IO5 has one run per setup, so no noise is measured there.']
    return '\n'.join(L) + '\n'


def analyze(dest):
    dest = Path(dest)
    s = analysis(load_runs(dest))
    (dest / 'summary.json').write_text(json.dumps(s, indent=1)); (dest / 'summary.md').write_text(markdown(s))
    print(markdown(s)); print('wrote', dest / 'summary.md', dest / 'summary.json')
    return 0 if s['verdict'] == 'PASS' else 1


# ---------------------------------------------------------------- self-check
def self_check():
    def run(i, setup, m, ok=True): return dict(id=f'{setup}-{i}', setup=setup, ok=ok, problems=[] if ok else ['x'], metrics=m, others_before=[], others_after=[])
    def mk(bw, lat): return {'seq-read-4k-q16/bw': bw, 'seq-read-4k-q16/lat': lat, 'mixed-4k-q16/bw': 1000.0}
    runs = {}
    # base: bw 100,102,98 (noise 4%), final bw 90 (ratio .9, loss 10%: beyond noise), latency 100 -> 80 (lower is better: ratio 1.25)
    for i, v in enumerate((100., 102., 98.)): r = run(i, 'base-raw', mk(v, 100.)); runs[r['id']] = r
    for i in range(3): r = run(i, 'final-raw', mk(90., 80.)); runs[r['id']] = r
    s = analysis(runs)['comparisons']['A']; rows = {r['job']: r for r in s['rows']}
    assert abs(rows['seq-read-4k-q16/bw']['ratio'] - 0.9) < 1e-9 and rows['seq-read-4k-q16/bw']['verdict'] == 'FLAG>5%'
    assert abs(rows['seq-read-4k-q16/lat']['ratio'] - 1.25) < 1e-9 and rows['seq-read-4k-q16/lat']['dir'] == 'lower'
    assert abs(rows['seq-read-4k-q16/bw']['noise'] - 0.04) < 1e-9
    assert abs(s['geomean_counted'] - math.sqrt(0.9)) < 1e-9, s['geomean_counted']   # latency is info; mixed is 1.0
    assert s['verdict'].startswith('PASS (loss 5-10%'), s['verdict']
    # 3% loss is inside the 4% noise: counted as 1.0
    for i in range(3): runs[f'final-raw-{i}']['metrics']['seq-read-4k-q16/bw'] = 97.
    s = analysis(runs)['comparisons']['A']; assert {r['job']: r for r in s['rows']}['seq-read-4k-q16/bw']['verdict'] == 'noise'
    assert abs(s['geomean_counted'] - 1.0) < 1e-9 and s['verdict'].startswith('PASS (aim')
    # 25% loss on one job: hard flag, FAIL-level geomean needs 2 jobs: sqrt(.75)=.866 -> loss 13% -> FAIL
    for i in range(3): runs[f'final-raw-{i}']['metrics']['seq-read-4k-q16/bw'] = 75.
    a = analysis(runs); s = a['comparisons']['A']
    assert 'seq-read-4k-q16/bw' in s['flags20'] and s['verdict'].startswith('FAIL') and a['verdict'] == 'FAIL'
    # a failed verification overrides speed
    for i in range(3): runs[f'final-raw-{i}']['metrics']['seq-read-4k-q16/bw'] = 110.
    assert analysis(runs)['verdict'] == 'PASS'
    runs['final-raw-0']['ok'] = False
    a = analysis(runs); assert a['verdict'] == 'FAIL' and a['comparisons']['A']['verdict'].startswith('FAIL (a run failed') and a['failed_runs']
    # overlay is not gated; IO5 time is lower-is-better
    io = lambda s, v: run(0, s, {'io5/solo-make': v, 'io5/make': 9.})
    a = analysis({'x': io('io5-raw', 100.), 'y': io('io5-overlay', 130.)})['comparisons']['D']
    assert a['verdict'].startswith('REPORT ONLY')
    assert [r['job'] for r in a['rows']] == ['io5/make', 'io5/solo-make']
    assert [r for r in a['rows'] if r['job'] == 'io5/solo-make'][0]['verdict'] == 'FLAG>20%' and a['flags20'] == ['io5/solo-make']
    # plan: ABBA order and counts
    p = build_plan(['A', 'BC', 'D'])
    assert ''.join('A' if s['setup'] == 'base-raw' else 'B' for s in p if s['group'] == 'A') == 'ABBAABBAAB' and len(p) == 21
    print('self-check passed: ratios, noise, geometric mean, direction, 5%/20% flags, failure override, gating, ABBA plan')
    return 0


# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('self-check')
    a = sub.add_parser('analyze'); a.add_argument('dir', type=Path)
    for name in ('plan', 'smoke', 'full'):
        s = sub.add_parser(name)
        # --baseline-build/--final-build: a natemu build folder laid out like cargo's target folder (release/natemu,
        # aarch64-unknown-none/release/natemu-runtime), as testkit's artifacts.natemu_at()/natemu() entries are
        s.add_argument('--baseline-dir', '--baseline-build', dest='baseline_dir', type=Path, default=PERF / 'baseline')
        s.add_argument('--final-dir', '--final-build', dest='final_dir', type=Path, default=PERF / 'final')
        s.add_argument('--fio-prep', type=Path, default=FIO_PREP, help='the fio preparation (run_io_matrix.py prepare --fio-prep)')
        s.add_argument('--block-inputs', type=Path, default=BLOCK_INPUTS, help='the block inputs (run_io_matrix.py prepare --block-inputs)')
        s.add_argument('--fexc', type=Path, default=FEXC); s.add_argument('--inputs', type=Path, default=PERF / 'inputs')
        s.add_argument('--io5-inputs', type=Path, default=IO5_INPUTS); s.add_argument('--lock', type=Path, default=VM_LOCK)
        s.add_argument('--final-commit', help='commit of the final binary (default: this checkout HEAD)')
        s.add_argument('--sets', default='A,BC,D', help='comma list of A, BC, D')
        s.add_argument('--dir', type=Path); s.add_argument('--retry-failed', action='store_true', help='rerun runs recorded as failed')
        if name == 'full':
            s.add_argument('--limit', type=int, help=f'run at most N pending runs, then exit {INCOMPLETE} if runs are left '
                           '(the test framework runs the case as pieces of one run each)')
            s.add_argument('--retry-failed-before', type=float, metavar='EPOCH',
                           help='rerun runs recorded as failed before this time (records without a time count as older)')
        if name == 'plan': s.add_argument('--count', action='store_true', help='print only the number of runs')
        if name == 'smoke': s.add_argument('--with-baseline', action='store_true', help='also one baseline raw run')
    args = p.parse_args()
    if args.cmd == 'self-check': return self_check()
    if args.cmd == 'analyze': return analyze(args.dir)
    if args.cmd == 'plan': return plan(args)
    if args.cmd == 'smoke':
        args.dir = args.dir or PERF / ('smoke-' + time.strftime('%Y%m%d-%H%M%S'))
        ids = (['final-raw-bc', 'final-qcow2'] + (['base-raw'] if args.with_baseline else []))
        return execute(args, args.dir, [dict(id=f'S-{s}-0', group='S', setup=s, rep=0, kind='matrix') for s in ids])
    if not args.dir: p.error('full needs --dir (the results directory; reuse it to resume)')
    return execute(args, args.dir, build_plan(args.sets.split(',')))


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main())
