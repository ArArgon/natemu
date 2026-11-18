"""Per-case results.json for the test framework (testkit/harness/runner.py:per_case):
{"results": [{"name", "outcome": match|fail, "diffs": [...]}]}, written from a runner's own judgement; it adds no check and
never says match where the runner's exit status says failure.

    write(out, cases, failures)          cases [(name, prefix)]: the failure lines beginning with a case's prefix are its
                                         diffs (x1/x3/x4/m3/s4)
    write_report(out, report, failures)  one case per report entry with `problems` (k1/l3/l4a/l4b/m2/r1/pic)
"""
import json


def _unique(name, taken):
    k = taken.get(name, 0)
    taken[name] = k + 1
    return name if not k else f'{name}#{k + 1}'


def write(out, cases, failures):
    """cases: [(name, prefix)]: the failure lines starting with `prefix` are that case's diffs."""
    results, taken, claimed = [], {}, set()
    for name, prefix in cases:
        mine = [i for i, f in enumerate(failures) if f.startswith(prefix)]
        claimed.update(mine)
        results.append({'name': _unique(name, taken), 'outcome': 'fail' if mine else 'match', 'diffs': [failures[i] for i in mine]})
    rest = [f for i, f in enumerate(failures) if i not in claimed]
    if rest: results.append({'name': 'other', 'outcome': 'fail', 'diffs': rest})
    (out / 'results.json').write_text(json.dumps({'results': results}, indent=1) + '\n')


def program_cases(mode, report):
    """x1/x3/x4 report entries: {'name', 'runs': [{'max_block', 'problems'}]} (the line `<name>: ...` is the QEMU answer's own
    check) or flat {'name', 'max_block'?, 'problems'}; failure lines `<name> (max-block L): p`, or `<name>: p` (x4, QEMU)."""
    cases = []
    for e in report:
        n = e['name']
        if 'runs' in e:
            if mode == 'qemu': cases.append((f'{mode}/{n}/qemu', f'{n}: '))
            cases += [(f"{mode}/{n}/max-block-{r['max_block']}", f"{n} (max-block {r['max_block']}): ") for r in e['runs']]
        elif 'max_block' in e: cases.append((f"{mode}/{n}/max-block-{e['max_block']}", f"{n} (max-block {e['max_block']}): "))
        else: cases.append((f'{mode}/{n}', f'{n}: '))
    return cases



def _entries(node, path):
    if isinstance(node, dict):
        if 'problems' in node or 'runs' in node:
            name = '/'.join(path + [str(node['name'])]) if 'name' in node else '/'.join(path + [f"block={node.get('block')}"])
            for r in node.get('runs') or []:
                yield from _entries(r, [name])
            if node.get('problems') or not node.get('runs'):
                yield name, node.get('problems') or []
        else:
            for k, v in node.items():
                if isinstance(v, (list, dict)): yield from _entries(v, path + [k])
    elif isinstance(node, list):
        for v in node: yield from _entries(v, path)


def write_report(out, report, failures):
    """From a report (a list of entries or a dict of such lists, an entry having `problems` and maybe `runs` with their
    own; k1/l3/l4a/l4b/m2/r1/pic): one case per entry. Failures the report does not show become an entry of their own."""
    rows = {}
    for name, problems in _entries(report, []):
        n, k = name, 1
        while n in rows:
            k += 1
            n = f'{name}#{k}'
        rows[n] = [str(p) for p in problems]
    if failures and not any(rows.values()):
        rows['failures'] = [str(f) for f in failures]
    results = [{'name': n, 'outcome': 'fail' if d else 'match', 'diffs': d} for n, d in rows.items()]
    (out / 'results.json').write_text(json.dumps({'results': results}, indent=1) + '\n')
