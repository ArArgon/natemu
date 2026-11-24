#!/usr/bin/env python3
"""The EL0 sampler's validity case (docs/restructure/el0-sampler-plan.md chunk 0; bench case guest-sampler-check-sample): a
sampling boot of the bench image's sampler_check program (tools/bench/image/src/sampler_check.c, `bench=sampler-check`),
then tests/bench/sample_report.py's report, then this judge of NAME.samples.

    python3 tests/bench/sampler_check_judge.py --profile NAME [--markers FILE] [--perf FILE] [-- COMMAND ...]

The program marks each phase on the control port (F9H 1-7, in order, in each of the boot's two runs; init's own mark, the
run's number, comes before them) and, in phase 5, stops (F8H 2) and starts (F8H 1) sampling. Each phase is the time from its
mark to the next mark; phase 7 ends at the next stop (init's, after the run). Samples are placed by their ring time. Per run:

  a  alu: at least 95% of the window's EL0 samples on one user block (the loop: its body and its linked back edge are one
     block). If this fails twice the plan goes back to review.
  b  call: dispatcher samples whose target is the callee (the not-a-call target sampled most), none of them marked a return,
     and samples on the callee's block.
  c  cross: samples in a shared probe, at least 95% of them charged to the window's top block (the loop's).
  d  chase: recorded only: where the samples land in the top block (native offset: samples), for the per-word allowance.
  e  ctrl: e1 (mark 5 to the port's stop) and e3 (the port's start to init's stop) hold EL0 samples on phase a's block;
     e2 (stop to start) holds no sample rung after the stop except by the ringing pass in flight at it (a logged pass that
     began before the stop); the stop and the start are the port's (cause port).
  marks: the seven marks in order in both runs; the control port ignored nothing (sample_report's own check).

Writes NAME.judge.json; exit 0 when the command, the report and every check pass, else the command's status or 1."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import collections
import json
import subprocess

import sample_report as sr

ROOT = pathlib.Path(__file__).resolve().parents[2]
ALU_SHARE = 95.0
PROBE_SHARE = 95.0
SEQUENCE = [1, 2, 3, 4, 5, 6, 7]


def read(name):
    """(ring samples as dicts with their vCPU, host records as dicts) of NAME.samples."""
    data = pathlib.Path(f'{name}.samples').read_bytes()
    ring, host, pending, vcpu = [], [], 0, 0
    for w in sr.REC.iter_unpack(data[sr.RECORD_BYTES:len(data) - len(data) % sr.RECORD_BYTES]):
        k = w[2] >> 60
        if pending:
            pending -= 1
            s = sr.sample(w); s['vcpu'] = vcpu; ring.append(s)
            continue
        h = sr.host(w)
        if k == sr.VCPU: vcpu, pending = h['value3'], h['value4']
        else: host.append(h)
    return ring, host


def runs(marks):
    """The mark times of each run: [[t1, ..., t7]] for each place the marks 1..7 appear in order."""
    vals = [m['value1'] for m in marks]
    out, i = [], 0
    while i + len(SEQUENCE) <= len(vals):
        if vals[i:i + len(SEQUENCE)] == SEQUENCE:
            out.append([m['time'] for m in marks[i:i + len(SEQUENCE)]]); i += len(SEQUENCE)
        else: i += 1
    return out


def key(s):
    return (s['rip'], s['frame'], s['context'])


def in_block(s):
    return s['kind'] == sr.EL0 and s['place'] in (sr.BLOCK, sr.PROBE, sr.STEP)


def between(ring, a, b):
    return [s for s in ring if a <= s['time'] < b]


def top_block(samples):
    c = collections.Counter(key(s) for s in samples if in_block(s))
    return c.most_common(1)[0] if c else (None, 0)


def judge_run(n, t, ring, toggles, passes, end):
    """One run's checks: {phase: {...}}, problems."""
    out, problems = {}, []
    win = lambda k: between(ring, t[k - 1], t[k] if k < 7 else end)
    el0 = lambda xs: [s for s in xs if s['kind'] == sr.EL0]
    # a
    a = el0(win(1))
    blk, n_blk = top_block(a)
    share = round(100 * n_blk / len(a), 2) if a else 0.0
    out['a'] = dict(el0=len(a), block=blk and [hex(x) for x in blk], block_samples=n_blk, share=share,
                    user=bool(blk) and blk[2] & 3 == 3)
    if share < ALU_SHARE or not out['a']['user']:
        problems.append(f'run {n} a: {share}% of {len(a)} EL0 samples on one user block, want {ALU_SHARE}% or more')
    alu = blk
    # b
    b = el0(win(2))
    disp = [s for s in b if s['place'] == sr.DISPATCH]
    calls = lambda s: s['flags'] >> sr.CALL_SHIFT & 3
    targets = collections.Counter(s['rip'] for s in disp if calls(s) == sr.CALL_NO)
    callee = targets.most_common(1)[0][0] if targets else None
    to_callee = [s for s in disp if s['rip'] == callee]
    marked = collections.Counter(sr.CALLS.get(calls(s)) for s in to_callee)
    on_callee = sum(1 for s in b if in_block(s) and s['rip'] == callee)
    out['b'] = dict(el0=len(b), dispatch=len(disp), callee=callee and hex(callee), slot_samples=len(to_callee),
                    slot_calls=dict(marked), callee_block_samples=on_callee,
                    dispatch_calls=dict(collections.Counter(sr.CALLS.get(calls(s)) for s in disp)))
    if not to_callee: problems.append(f'run {n} b: no dispatcher sample whose target is an indirect call\'s')
    elif marked.get('return'): problems.append(f'run {n} b: {marked["return"]} of the callee\'s slot samples marked a return')
    if not on_callee: problems.append(f'run {n} b: no sample on the callee\'s block')
    # c
    c = el0(win(3))
    blk_c, _ = top_block(c)
    probes = [s for s in c if s['place'] == sr.PROBE]
    charged = sum(1 for s in probes if key(s) == blk_c)
    pshare = round(100 * charged / len(probes), 2) if probes else 0.0
    out['c'] = dict(el0=len(c), probe=len(probes), charged_to_loop=charged, share=pshare, block=blk_c and [hex(x) for x in blk_c])
    if not probes: problems.append(f'run {n} c: no probe sample')
    elif pshare < PROBE_SHARE: problems.append(f'run {n} c: {pshare}% of probe samples charged to the loop\'s block, want {PROBE_SHARE}%')
    # d (recorded)
    d = el0(win(4))
    blk_d, n_d = top_block(d)
    at = collections.Counter(sr.offset(s) for s in d if in_block(s) and key(s) == blk_d)
    out['d'] = dict(el0=len(d), block=blk_d and [hex(x) for x in blk_d], block_samples=n_d, at=dict(sorted(at.items())))
    # e
    stop = next((x for x in toggles if t[4] <= x['time'] < t[5] and not x['value2']), None)
    start = next((x for x in toggles if t[5] <= x['time'] < t[6] and x['value2']), None)
    if not stop or not start:
        problems.append(f'run {n} e: the port\'s stop and start were not found between marks 5, 6 and 7')
        return out, problems
    if stop['value3'] != 1 or start['value3'] != 1: problems.append(f'run {n} e: the stop or the start is not the port\'s')
    in_flight = [p for p in passes if p['time'] <= stop['time'] <= p['value1']]
    allowed = max([p['value1'] for p in in_flight], default=stop['time'])
    e1, e2, e3 = between(ring, t[4], stop['time']), between(ring, stop['time'], start['time']), between(ring, start['time'], end)
    stray = [s for s in e2 if s['time'] > allowed]
    on = lambda xs: sum(1 for s in xs if in_block(s) and key(s) == alu)
    out['e'] = dict(e1=len(e1), e1_on_loop=on(e1), e2=len(e2), e2_in_flight=len(e2) - len(stray), e2_stray=len(stray),
                    e3=len(e3), e3_on_loop=on(e3), stop_seq=stop['value1'], start_seq=start['value1'])
    if stray: problems.append(f'run {n} e: {len(stray)} samples rung in the stopped window after the pass in flight')
    if not on(e1): problems.append(f'run {n} e: no EL0 sample on the loop before the stop')
    if not on(e3): problems.append(f'run {n} e: no EL0 sample on the loop after the start (the sampler did not start again)')
    return out, problems


def judge(name):
    ring, host = read(name)
    marks = sorted((h for h in host if h['kind'] == sr.MARK), key=lambda h: h['time'])
    toggles = sorted((h for h in host if h['kind'] == sr.TOGGLE), key=lambda h: (h['time'], h['value1']))
    passes = [h for h in host if h['kind'] == sr.PASS]
    clocks = [h['time'] for h in host if h['kind'] == sr.CLOCK]
    found = runs(marks)
    res, problems = dict(marks=[m['value1'] for m in marks], runs={}), []
    if len(found) != 2: problems.append(f'marks 1-7 in order {len(found)} times, want 2 (one per run): {res["marks"]}')
    for n, t in enumerate(found, 1):
        end = next((x['time'] for x in toggles if x['time'] > t[6] and not x['value2']), max(clocks, default=t[6]))
        r, p = judge_run(n, t, ring, toggles, passes, end)
        res['runs'][n] = r; problems += p
    res['problems'] = problems
    return res


def main(argv):
    command = []
    if '--' in argv:
        i = argv.index('--'); argv, command = argv[:i], argv[i + 1:]
    if len(argv) < 2 or argv[0] != '--profile': sys.exit(__doc__)
    name = argv[1]
    code = subprocess.run(command, cwd=ROOT).returncode if command else 0
    problems = sr.main(argv) if pathlib.Path(f'{name}.samples').exists() else 1   # the report, its own checks
    res = judge(name) if pathlib.Path(f'{name}.samples').exists() else dict(problems=[f'{name}.samples: missing'])
    pathlib.Path(f'{name}.judge.json').write_text(json.dumps(res, indent=1) + '\n')
    for n, r in res.get('runs', {}).items():
        print(f'run {n}: ' + '; '.join(f'{k} {json.dumps(v)}' for k, v in r.items()), flush=True)
    for p in res['problems']: print('problem:', p, flush=True)
    return code if code else 1 if problems or res['problems'] else 0


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main(sys.argv[1:]))
