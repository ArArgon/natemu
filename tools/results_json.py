"""Per-case results.json for the test framework (testkit/harness/runner.py:per_case) from the group reports of
run_k2.py and run_nostop.py: {"results": [{"name", "outcome": match|fail|unsupported|excluded, "diffs": [...]}]}.

It only restates what the runner already judged (a run's `ok`/`problems`, the group's failure lines); it adds no check.
A run that carries `excluded` (a documented deviation excluded by name, e.g. masked-sse-de) is reported `excluded`.
A group without runs (cost, rdrand) is one entry named after the group. Failure lines that no run explains (e.g.
stifirst reuse-copy) are reported as one failing entry `<group>`, so any failure line shows up in the file."""
import json


def _name(group, r, taken):
    parts = [str(r.get('case') or r.get('mode') or 'run')]
    if r.get('vcpus') is not None and f"vcpus={r['vcpus']}" not in parts[0]: parts.append(f"v{r['vcpus']}")
    if 'block' in r and f"-{r['block']}" not in parts[0]: parts.append(f"b{r['block']}")
    n = f"{group}/{'-'.join(parts)}"
    k = taken.get(n, 0)
    taken[n] = k + 1
    return n if not k else f'{n}#{k + 1}'


def write(out, report):
    results, taken = [], {}
    for group, g in report.items():
        failures = list(g.get('failures') or [])
        runs = g.get('runs')
        if not runs:
            results.append({'name': group, 'outcome': 'fail' if failures else 'match', 'diffs': failures})
            continue
        failed = 0
        for r in runs:
            case = str(r.get('case') or r.get('mode') or '')
            own = [f for f in failures if f.startswith(f'{group} {case}') and f"block={r.get('block')}" in f] if 'ok' not in r else []
            bad = r.get('ok') is False or bool(r.get('problems')) or bool(own)
            failed += bad
            if bad: outcome, diffs = 'fail', [str(p) for p in (r.get('problems') or own or [r])]
            elif r.get('excluded'): outcome, diffs = 'excluded', [f"excluded by name: {', '.join(sorted(r['excluded']))}"]
            else: outcome, diffs = 'match', []
            results.append({'name': _name(group, r, taken), 'outcome': outcome, 'diffs': diffs})
        if failures and not failed:
            results.append({'name': group, 'outcome': 'fail', 'diffs': failures})
    (out / 'results.json').write_text(json.dumps({'results': results}, indent=1) + '\n')
