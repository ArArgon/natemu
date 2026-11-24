#!/usr/bin/env python3
"""The computed-exit census reduced to the decision numbers (docs/restructure/el0-sampler-plan.md, chunk 2 and section
7), from the census plugin's JSON (tests/bench/plugins/indirect.c) of one segment (a benchmark's run 1, an io5 step):

- the D1 input: the share of indirect jmp and call executions that go to their site's main target (top-1) and to its
  two most frequent targets (top-2), execution-weighted, user, kernel and total. Per address space (a site is its
  address space and address), and by code: sites merged by their physical code with each target taken relative to the
  site's own address (target - pc), as natemu's translated code is shared between processes by frame and its
  position-independent templates would compare page-relative targets. The plugin keeps the first 4 targets a site
  instance saw and counts the rest as overflow, so top-1 and top-2 are lower bounds, by at most the overflow share
  (also given). The share at sites that saw exactly one target is kept as a secondary row (it falls with run length:
  a site going to one target 99.9% of the time is not in it);
- ret executions, and the return-stack model's hit rate at depths 8, 16 and 32 (a pop of an empty stack is a miss),
  for user and kernel returns apart: one stack per vCPU, and split (one per vCPU and mode, so a system call's kernel
  calls do not overwrite user entries);
- each per 1,000 guest instructions executed (time under TCG means nothing here);
- REP MOVS and STOS (the bulk REP plan's task 0, docs/restructure/bulk-rep-plan.md section 10): per form (op, width,
  code mode, privilege, variant) the instances and bytes, their split by RCX, alignment, overlap and DF, and the bytes
  eligible for a bulk loop and those it would move; and the plan's decision numbers: of the 64-bit MOVSB and MOVSQ
  bytes, the share eligible and the share a bulk loop would move (task 0 stops the first cut under half), and the same
  for 32-bit MOVSD (section 3.2).

    python3 tests/bench/census_report.py CENSUS.json [--window run1] [--json OUT.json]

run_census.py calls reduce() and markdown() for each case; run as a program it refuses outside the framework."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import json

MODES = ('user', 'kernel')
CLASSES = ('1', '2', '3', '4', '5+')
DEPTHS = ('8', '16', '32')
RAS = ('ras', 'ras_split')
COLS = ('kind', 'mode', 'asid', 'pc', 'phys', 'flags', 'execs', 'overflow', 'targets')
REP_KEY = ('op', 'width', 'bits', 'mode', 'variant')
REP_COUNTS = ('instances', 'bytes', 'inst_rcx0', 'inst_under_round', 'inst_1_16_rounds', 'inst_more', 'bytes_rcx0',
              'bytes_under_round', 'bytes_1_16_rounds', 'bytes_more', 'inst_aligned8', 'bytes_aligned8', 'inst_aligned4',
              'bytes_aligned4', 'inst_overlap', 'bytes_overlap', 'inst_df', 'bytes_df', 'inst_eligible', 'bytes_eligible',
              'bytes_bulk')
VARIANTS = {1: 'a32', 2: 'seg', 4: 'f2'}
# The plan's groups: the first cut (64-bit MOVSB and MOVSQ, user and kernel) and section 3.2's 32-bit MOVSD.
REP_GROUPS = {'first_cut': lambda f: f['op'] == 'movs' and f['bits'] == 64 and f['width'] in (1, 8),
              'movsd32': lambda f: f['op'] == 'movs' and f['bits'] == 32 and f['width'] == 4}


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


def top(site: dict, n: int) -> int:
    """Executions to the site's n most frequent kept targets."""
    return sum(sorted((c for _, c in site['targets']), reverse=True)[:n])


def by_targets(ss: list[dict]) -> dict:
    execs = sum(s['execs'] for s in ss)
    share = lambda n: n / execs if execs else None
    cls = {c: sum(s['execs'] for s in ss if distinct(s) == c) for c in CLASSES}
    return {'execs': execs, 'sites': len(ss),
            'top1': share(sum(top(s, 1) for s in ss)), 'top2': share(sum(top(s, 2) for s in ss)),
            'overflow': share(sum(s['overflow'] for s in ss)),
            'execs_by_targets': cls, 'sites_by_targets': {c: sum(1 for s in ss if distinct(s) == c) for c in CLASSES},
            'one_target_sites': share(cls['1']), 'two_target_sites': share(cls['2'])}


def physical(s: dict) -> tuple:
    return (s['kind'], s['mode'], s['phys'] or s['pc'])


def relative(ss: list[dict]) -> list[dict]:
    """The sites with each target as its distance from the site's own address."""
    return [dict(s, targets=[(t - s['pc'], c) for t, c in s['targets']]) for s in ss]


def rep_forms(seg: dict) -> list[dict]:
    return [dict(zip(REP_KEY + REP_COUNTS, r)) for r in seg.get('rep', [])]


def rep_label(f: dict) -> str:
    suffix = {1: 'b', 2: 'w', 4: 'd', 8: 'q'}[f['width']]
    var = '+'.join(n for b, n in VARIANTS.items() if f['variant'] & b)
    return f"rep {f['op']}{suffix} {f['bits']}-bit {f['mode']}" + (f' {var}' if var else '')


def rep_summary(forms: list[dict]) -> dict:
    """The forms' counts summed, and the eligible and bulk shares of their bytes."""
    tot = {k: sum(f[k] for f in forms) for k in REP_COUNTS}
    share = lambda k: tot[k] / tot['bytes'] if tot['bytes'] else None
    return {**tot, 'eligible_share': share('bytes_eligible'), 'bulk_share': share('bytes_bulk'),
            'aligned8_share': share('bytes_aligned8'), 'overlap_share': share('bytes_overlap')}


def reduce_rep(seg: dict) -> dict:
    forms = rep_forms(seg)
    rows = sorted(({'form': rep_label(f), **f} for f in forms), key=lambda f: -f['bytes'])
    groups = {g: {m: rep_summary([f for f in forms if pick(f) and m in ('total', f['mode'])]) for m in (*MODES, 'total')}
              for g, pick in REP_GROUPS.items()}
    return {'forms': rows, 'groups': groups, 'continued': seg.get('rep_continued'), 'df_unknown': seg.get('rep_df_unknown')}


def merge_segments(segs: list[dict], name: str) -> dict:
    """Segments summed into one (counts added; sites merged by kind, address space and address)."""
    out = {'name': name, 'insns': {m: 0 for m in MODES}, 'direct_calls': {m: 0 for m in MODES},
           **{k: {d: {m: {'hit': 0, 'miss': 0, 'empty': 0} for m in MODES} for d in DEPTHS} for k in RAS},
           'events': {}, 'far': {k: {m: 0 for m in MODES} for k in ('call', 'jmp', 'ret')}}
    for s in segs:
        for m in MODES:
            out['insns'][m] += s['insns'][m]; out['direct_calls'][m] += s['direct_calls'][m]
            for k in out['far']: out['far'][k][m] += s['far'][k][m]
            for k in RAS:
                for d in DEPTHS:
                    for r in ('hit', 'miss', 'empty'): out[k][d][m][r] += s[k][d][m][r]
        for k, v in s['events'].items(): out['events'][k] = out['events'].get(k, 0) + v
    merged = regroup([x for s in segs for x in sites(s)], lambda x: (x['kind'], x['mode'], x['asid'], x['pc']))
    out['sites'] = [[m[c] for c in COLS] for m in merged]
    reps = {}
    for s in segs:
        for r in s.get('rep', []):
            k = tuple(r[:len(REP_KEY)])
            reps[k] = [a + b for a, b in zip(reps.get(k, [0] * len(REP_COUNTS)), r[len(REP_KEY):])]
    out['rep'] = [[*k, *v] for k, v in reps.items()]
    out['rep_continued'] = sum(s.get('rep_continued', 0) for s in segs)
    out['rep_df_unknown'] = sum(s.get('rep_df_unknown', 0) for s in segs)
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
         'events': seg['events'], 'modes': {}, 'rep_registers': data.get('rep'), 'rep': reduce_rep(seg)}
    for mode in (*MODES, 'total'):
        mine = [s for s in ss if mode == 'total' or s['mode'] == mode]
        ind = [s for s in mine if s['kind'] in ('call', 'jmp')]
        insns = sum(seg['insns'].values()) if mode == 'total' else seg['insns'][mode]
        ras = {key: {d: {k: sum(seg[key][d][m][k] for m in MODES if mode in ('total', m)) for k in ('hit', 'miss', 'empty')}
                     for d in DEPTHS} for key in RAS}
        calls = sum(s['execs'] for s in ind if s['kind'] == 'call')
        jmps = sum(s['execs'] for s in ind if s['kind'] == 'jmp')
        rets = sum(s['execs'] for s in mine if s['kind'] == 'ret')
        direct = sum(seg['direct_calls'][m] for m in MODES if mode in ('total', m))
        per = lambda n: 1000 * n / insns if insns else None
        r['modes'][mode] = {
            'insns': insns,
            'indirect': by_targets(ind), 'indirect_call': by_targets([s for s in ind if s['kind'] == 'call']),
            'indirect_jmp': by_targets([s for s in ind if s['kind'] == 'jmp']),
            'indirect_by_code': by_targets(regroup(relative(ind), physical)),
            'indirect_call_by_code': by_targets(regroup(relative([s for s in ind if s['kind'] == 'call']), physical)),
            'indirect_jmp_by_code': by_targets(regroup(relative([s for s in ind if s['kind'] == 'jmp']), physical)),
            'ret': by_targets([s for s in mine if s['kind'] == 'ret']),
            **{key: {d: {**v, 'hit_rate': v['hit'] / sum(v.values()) if sum(v.values()) else None} for d, v in ras[key].items()}
               for key in RAS},
            'per_1000': {'indirect_call': per(calls), 'indirect_jmp': per(jmps), 'indirect': per(calls + jmps),
                         'ret': per(rets), 'direct_call': per(direct)},
        }
    return r


def _pct(x): return '' if x is None else f'{100 * x:.1f}%'
def _pair(b): return '' if b['top1'] is None else f"{_pct(b['top1'])} / {_pct(b['top2'])}"
def _num(x): return '' if x is None else f'{x:.2f}'


def markdown(r: dict, name: str) -> str:
    m = r['modes']
    rows = [('guest instructions', lambda v: f'{v["insns"]:,}'),
            ('indirect jmp + call per 1,000 instructions', lambda v: _num(v['per_1000']['indirect'])),
            ('  of which calls', lambda v: _num(v['per_1000']['indirect_call'])),
            ('indirect executions to the main target / top two, per address space', lambda v: _pair(v['indirect'])),
            ('  calls', lambda v: _pair(v['indirect_call'])),
            ('  jmps', lambda v: _pair(v['indirect_jmp'])),
            ('indirect executions to the main target / top two, by code (targets relative to the site)',
             lambda v: _pair(v['indirect_by_code'])),
            ('  calls', lambda v: _pair(v['indirect_call_by_code'])),
            ('  jmps', lambda v: _pair(v['indirect_jmp_by_code'])),
            ('indirect executions beyond the 4 kept targets (bound on the above)', lambda v: _pct(v['indirect']['overflow'])),
            ('secondary: indirect executions at sites that saw exactly one target', lambda v: _pct(v['indirect']['one_target_sites'])),
            ('indirect sites (1 / 2 / 3 / 4 / 5+ targets)', lambda v: ' / '.join(str(v['indirect']['sites_by_targets'][c]) for c in CLASSES)),
            ('ret per 1,000 instructions', lambda v: _num(v['per_1000']['ret'])),
            ('direct call per 1,000 instructions', lambda v: _num(v['per_1000']['direct_call']))]
    rows += [('return hit rate, one stack, depth 8 / 16 / 32',
              lambda v: ' / '.join(_pct(v['ras'][d]['hit_rate']) for d in DEPTHS)),
             ('return hit rate, user and kernel stacks apart, depth 8 / 16 / 32',
              lambda v: ' / '.join(_pct(v['ras_split'][d]['hit_rate']) for d in DEPTHS))]
    out = [f'### Census {name}, segment {r["window"]}', '', '| | user | kernel | total |', '| --- | ---: | ---: | ---: |']
    out += [f'| {label} | ' + ' | '.join(f(m[k]) for k in ('user', 'kernel', 'total')) + ' |' for label, f in rows]
    out += ['', f'CR3 {r["cr3"]}; RIP {r["rip"]}; events {r["events"]}.', '']
    out += rep_markdown(r)
    return '\n'.join(out)


def _mib(n): return f'{n / 2 ** 20:,.2f}'


def rep_markdown(r: dict) -> list[str]:
    rp = r.get('rep')
    if not rp or not rp['forms']: return [f'No REP MOVS or STOS counted (registers: {r.get("rep_registers")}).', '']
    out = ['REP MOVS and STOS by form, by bytes (the first iteration\'s RCX times the width; a round is 16 bytes, 8 in 32-bit '
           'code; eligible: the bulk rule of the form\'s plan; bulk: the bytes that loop would move):', '',
           '| form | instances | MiB | RCX 0 / < round / 1-16 rounds / more, % of instances | 8-aligned | overlap | DF | '
           'eligible | bulk |', '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for f in rp['forms'][:24]:
        b = f['bytes'] or 1
        inst = ' / '.join(f"{100 * f[k] / f['instances']:.0f}" for k in REP_COUNTS[2:6])
        out.append(f"| {f['form']} | {f['instances']:,} | {_mib(f['bytes'])} | {inst} | {_pct(f['bytes_aligned8'] / b)} | "
                   f"{_pct(f['bytes_overlap'] / b)} | {_pct(f['bytes_df'] / b)} | {_pct(f['bytes_eligible'] / b)} | "
                   f"{_pct(f['bytes_bulk'] / b)} |")
    out += ['', '| group, % of its bytes | user | kernel | total |', '| --- | ---: | ---: | ---: |']
    for g, label in (('first_cut', '64-bit MOVSB + MOVSQ'), ('movsd32', '32-bit MOVSD')):
        v = rp['groups'][g]
        out.append(f'| {label}: MiB | ' + ' | '.join(_mib(v[m]['bytes']) for m in (*MODES, 'total')) + ' |')
        out.append(f'| {label}: eligible | ' + ' | '.join(_pct(v[m]['eligible_share']) for m in (*MODES, 'total')) + ' |')
        out.append(f'| {label}: moved in bulk | ' + ' | '.join(_pct(v[m]['bulk_share']) for m in (*MODES, 'total')) + ' |')
    out += ['', f'Continued instances (QEMU re-entered one): {rp["continued"]:,}; instances whose direction was not found (taken '
            f'as DF 0): {rp.get("df_unknown") or 0:,}.', '']
    return out


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
