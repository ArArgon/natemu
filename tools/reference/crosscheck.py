#!/usr/bin/env python3
"""QEMU's answers that a static expectation replaced (Part C of docs/superpowers/plans/2026-11-18-campaign-onboarding.md:
where the program decides the right value, the test writes it down). When a runner stops asking QEMU, its answers leave
the family's tests.lock and their projection hashes are kept here, one file per runner (tools/reference/crosscheck/
<row>.json: {"pin": ..., "answers": {key: sha256}}), so that the expectation's unit test can show it equals what QEMU
answered. No run reads them.

    PYTHONPATH=. <natemu-test's venv>/bin/python tools/reference/crosscheck.py take tests/system k1 KEYS.json
        moves the keys in KEYS.json (a JSON list) and every answer derived from them (key#..., key/...) out of the lock,
        the keys' own hashes into crosscheck/k1.json; [repro] sets no answer uses any more go too

In a unit test: `crosscheck.agrees('k1', key, projection)` -> None, or why not."""
import hashlib, json, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
DIR = HERE / 'crosscheck'


def load(row):
    return json.loads((DIR / f'{row}.json').read_text())


def agrees(row, key, projection):
    """None when QEMU's recorded answer of `key` hashes like `projection` (canonical bytes, answers.py), else a reason."""
    got = load(row)['answers'].get(key)
    if got is None: return f'no QEMU answer recorded for {key} (the program changed since: check the expectation by other means)'
    return None if hashlib.sha256(projection).hexdigest() == got else f'differs from QEMU\'s answer {key}'


def take(family_dir, row, keys):
    sys.path.insert(0, str(HERE.parent.parent))
    from testkit import family, lock
    fam = family.load(pathlib.Path(family_dir).resolve() / 'family.toml')
    data = lock.read(fam)
    ans = data.get('answers', {})
    missing = [k for k in keys if k not in ans]
    if missing: sys.exit(f'{len(missing)} keys not in {fam.name} lock: {missing[:3]}')
    path = DIR / f'{row}.json'
    old = json.loads(path.read_text()) if path.exists() else {'answers': {}}
    rec = {**old['answers'], **{k: ans[k]['sha256'] for k in keys}}
    pins = {data['repro'][ans[k]['repro'].split(' ')[0]]['pin'] for k in keys}
    if len(pins) != 1: sys.exit(f'answers of several QEMU pins: {pins}')
    DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps({'pin': pins.pop(), 'family': fam.name, 'answers': dict(sorted(rec.items()))}, indent=1) + '\n')
    gone = {k for k in ans if any(k == x or k.startswith(x + '#') or k.startswith(x + '/') for x in keys)}
    data['answers'] = {k: v for k, v in ans.items() if k not in gone}
    used = {v['repro'].split(' ')[0] for v in data['answers'].values() if isinstance(v.get('repro'), str)}
    data['repro'] = {k: v for k, v in data.get('repro', {}).items() if k in used}
    lock.write(fam, data)
    print(f'{fam.name}: {len(gone)} answers out of the lock ({len(keys)} recorded in {path.relative_to(HERE.parent.parent)}); {len(data["answers"])} left')


if __name__ == '__main__':
    if len(sys.argv) != 5 or sys.argv[1] != 'take': sys.exit(__doc__)
    take(sys.argv[2], sys.argv[3], json.loads(pathlib.Path(sys.argv[4]).read_text()))
