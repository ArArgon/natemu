#!/usr/bin/env python3
"""The EL0 sampler's validity case (docs/restructure/el0-sampler-plan.md chunk 0; bench case guest-sampler-check-sample): a
sampling boot of the bench image's sampler_check program (tools/bench/image/src/sampler_check.c, `bench=sampler-check`),
then tests/bench/sample_report.py's report, then this judge of NAME.samples.

    python3 tests/bench/sampler_check_judge.py --profile NAME [--markers FILE] [--perf FILE] [-- COMMAND ...]

The program marks each phase on the control port (F9H 1-7, in order, in each of the boot's two runs; init's own mark, the
run's number, comes before them) and, in phase 5, stops (F8H 2) and starts (F8H 1) sampling. Each phase is the time from its
mark to the next mark; phase 7 ends at the next stop (init's, after the run). Samples are placed by their ring time. Per run:

  a  alu: at least 95% of the window's EL0 samples on one user block (the loop: its body and its linked back edge are one
     block), and 95% of those locate (sample_report.locate) to a word of its object. If this fails twice the plan goes back
     to review.
  b  call: both computed exits of the loop in the dispatcher: the indirect call (call test "not a call"), at least 90% of
     those to one target, the callee (small_fn: the not-a-call target sampled most), none of the callee's slot samples
     marked a return; and small_fn's `ret` (call test "call before": its target follows a call); and samples on the
     callee's block.
  c  cross: samples in a shared probe, at least 95% of them charged to the loop's block (the top block of the window's
     samples in a block, probes not counted); over both runs together (the crossing store is nearly all resident time, so
     one run's window can hold none).
  d  chase: recorded only: where the samples land in the top block (native offset: samples), for the per-word allowance.
  e  ctrl: e1 (mark 5 to the port's stop) and e3 (the port's start to init's stop) hold EL0 samples on phase a's block;
     e2 (stop to start) holds no sample rung after the stop except by the ringing pass in flight at it (a logged pass that
     began before the stop), no logged pass that rang a bell and no halted sample after that pass; the stop and the start
     are the port's (cause port).
  f  cold exits (split-check plan step A): linker entries through a split block's cold code over the boot (natemu's
     `cold_exits` on NAME.stderr) under 1% of the cross phase's iterations in both runs (NAME.serial's end lines): the probe
     returns into the hot copy, whose exits are linked. Judged when NAME.stderr exists.
  marks: the seven marks in order in both runs (init's own, 0x80 + the run's number, ignored; an older init's, the run's
     number before mark 1, is skipped by the search); the control port ignored nothing (sample_report's own check).

Writes NAME.judge.json; exit 0 when the command, the report and every check pass, else the command's status or 1."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import collections
import json
import re
import subprocess

import sample_report as sr

ROOT = pathlib.Path(__file__).resolve().parents[2]
ALU_SHARE = 95.0
COLD_SHARE = 1.0           # f: cold exits per cross iteration, at most (%)
PROBE_SHARE = 95.0
CALL_SHARE = 90.0
INIT_MARKS = 0x80          # init's marks, 0x80 + the run's number: not the program's phases
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


def judge_run(n, t, ring, toggles, passes, end, halted=(), objects=None):
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
    # a, end to end: the loop's samples find the loop's object and a word of its code (sample_report.locate).
    if objects is None: problems.append(f'run {n} a: no objects file')
    elif blk:
        found = [sr.locate(objects, s) for s in a if in_block(s) and key(s) == blk]
        words = sum(1 for x in found if x and x['word'] is not None)
        out['a'].update(located=sum(1 for x in found if x), words=words)
        if 100 * words < ALU_SHARE * len(found):
            problems.append(f'run {n} a: {words} of the loop\'s {len(found)} samples locate to a word of its object, want {ALU_SHARE}%')
    # b
    b = el0(win(2))
    disp = [s for s in b if s['place'] == sr.DISPATCH]
    calls = lambda s: s['flags'] >> sr.CALL_SHIFT & 3
    targets = collections.Counter(s['rip'] for s in disp if calls(s) == sr.CALL_NO)
    callee = targets.most_common(1)[0][0] if targets else None
    to_callee = [s for s in disp if s['rip'] == callee]
    marked = collections.Counter(sr.CALLS.get(calls(s)) for s in to_callee)
    on_callee = sum(1 for s in b if in_block(s) and s['rip'] == callee)
    returns = sum(1 for s in disp if calls(s) == sr.CALL_YES)
    cshare = round(100 * targets[callee] / sum(targets.values()), 2) if targets else 0.0
    out['b'] = dict(el0=len(b), dispatch=len(disp), callee=callee and hex(callee), slot_samples=len(to_callee),
                    slot_calls=dict(marked), call_share_to_callee=cshare, returns=returns, callee_block_samples=on_callee,
                    dispatch_calls=dict(collections.Counter(sr.CALLS.get(calls(s)) for s in disp)))
    if not to_callee: problems.append(f'run {n} b: no dispatcher sample whose target is an indirect call\'s')
    elif marked.get('return'): problems.append(f'run {n} b: {marked["return"]} of the callee\'s slot samples marked a return')
    elif cshare < CALL_SHARE: problems.append(f'run {n} b: {cshare}% of the indirect calls\' samples to the callee, want {CALL_SHARE}%')
    if not returns: problems.append(f'run {n} b: no dispatcher sample of the callee\'s return (call test "call before")')
    if not on_callee: problems.append(f'run {n} b: no sample on the callee\'s block')
    # c
    c = el0(win(3))
    blk_c, _ = top_block([s for s in c if s['place'] == sr.BLOCK])     # the loop's own words, not its probe's
    probes = [s for s in c if s['place'] == sr.PROBE]
    charged = sum(1 for s in probes if key(s) == blk_c)
    pshare = round(100 * charged / len(probes), 2) if probes else 0.0
    out['c'] = dict(el0=len(c), probe=len(probes), charged_to_loop=charged, share=pshare, block=blk_c and [hex(x) for x in blk_c])
    # (judged over both runs, in `judge`: the crossing store spends about 99% of its time outside generated code today, so
    # one run's window may hold no probe sample at all)
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
    rang = [p for p in passes if allowed < p['time'] < start['time'] and p['value4']]
    looked = [h for h in halted if allowed < h['time'] < start['time']]
    out['e'].update(e2_passes_rung=len(rang), e2_halted=len(looked))
    if stray: problems.append(f'run {n} e: {len(stray)} samples rung in the stopped window after the pass in flight')
    if rang: problems.append(f'run {n} e: {len(rang)} ringing passes rang bells in the stopped window')
    if looked: problems.append(f'run {n} e: {len(looked)} halted samples in the stopped window')
    if not on(e1): problems.append(f'run {n} e: no EL0 sample on the loop before the stop')
    if not on(e3): problems.append(f'run {n} e: no EL0 sample on the loop after the start (the sampler did not start again)')
    return out, problems


def judge(name):
    ring, host = read(name)
    marks = sorted((h for h in host if h['kind'] == sr.MARK and h['value1'] < INIT_MARKS), key=lambda h: h['time'])
    toggles = sorted((h for h in host if h['kind'] == sr.TOGGLE), key=lambda h: (h['time'], h['value1']))
    passes = [h for h in host if h['kind'] == sr.PASS]
    halted = [h for h in host if h['kind'] == sr.HALTED]
    objects = sr.Objects(f'{name}.objects') if pathlib.Path(f'{name}.objects').exists() else None
    clocks = [h['time'] for h in host if h['kind'] == sr.CLOCK]
    found = runs(marks)
    res, problems = dict(marks=[m['value1'] for m in marks], runs={}), []
    if len(found) != 2: problems.append(f'marks 1-7 in order {len(found)} times, want 2 (one per run): {res["marks"]}')
    for n, t in enumerate(found, 1):
        end = next((x['time'] for x in toggles if x['time'] > t[6] and not x['value2']), max(clocks, default=t[6]))
        r, p = judge_run(n, t, ring, toggles, passes, end, halted, objects)
        res['runs'][n] = r; problems += p
    probes = sum(r['c']['probe'] for r in res['runs'].values())
    charged = sum(r['c']['charged_to_loop'] for r in res['runs'].values())
    res['c'] = dict(probe=probes, charged_to_loop=charged, share=round(100 * charged / probes, 2) if probes else 0.0)
    if not probes: problems.append('c: no probe sample in either run')
    elif res['c']['share'] < PROBE_SHARE:
        problems.append(f'c: {res["c"]["share"]}% of probe samples charged to the loop\'s block, want {PROBE_SHARE}%')
    if pathlib.Path(f'{name}.stderr').exists():
        res['f'], p = cold_exits(name); problems += p
    res['problems'] = problems
    return res


def cold_exits(name):
    """Check f: (the counts, problems) from NAME.stderr's `cold_exits` (summed over vCPUs) and NAME.serial's cross end lines."""
    err = pathlib.Path(f'{name}.stderr').read_text(errors='replace')
    serial = pathlib.Path(f'{name}.serial')
    cold = [int(x) for x in re.findall(r'natemu-system-counts vcpu \d+ .* cold_exits (\d+)', err)]
    iters = [int(x) for x in re.findall(r'NATEMU-SAMPLER end run=\d+ phase=\d+ name=cross .*?iters=(\d+)',
                                        serial.read_text(errors='replace') if serial.exists() else '')]
    out = dict(cold_exits=sum(cold) if cold else None, cross_iterations=sum(iters), runs=len(iters))
    if not cold: return out, ['f: natemu printed no cold_exits count']
    if len(iters) != 2: return out, [f'f: {len(iters)} cross end lines on the serial console, want 2']
    out['share'] = round(100 * out['cold_exits'] / out['cross_iterations'], 4) if out['cross_iterations'] else None
    if out['share'] is None or out['share'] >= COLD_SHARE:
        return out, [f"f: {out['cold_exits']} cold exits over {out['cross_iterations']} cross iterations, want under {COLD_SHARE}%"]
    return out, []


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
