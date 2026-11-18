"""Per-family `tests.lock` (spec R2): derived hashes only, written by `natemu-test lock`, never by hand.

    [input.<name>]           fingerprint (generator, its sources, args), output (sha256 of the canonical built input)
    [suite.<name>]           fingerprint (input fingerprint, guest-visible settings, normalizer, reference pin), reference
    [suite.<name>.seeded]    tool, pin, machine, date: how the answers were produced (R2.8)
    [suite.<name>.cases]     case = answer sha256 ("" = no answer yet: the case reports stale-lock)
    [suite.<name>.excluded]  variant id = [cases the family's rule excludes] (compared at collection, R2.13)

Fingerprints come only from files in the repo and only from what decides the answer (R2.7): never `needs`, `smoke`,
`batch` or other scheduling fields, never a machine's local tools.
"""
import json
import pathlib
import tomllib

import tomli_w

from testkit import cache, paths
from testkit.family import Family, Suite


def path_of(fam: Family) -> pathlib.Path:
    return fam.dir / 'tests.lock'


def read(fam: Family) -> dict:
    p = path_of(fam)
    return tomllib.loads(p.read_text()) if p.exists() else {}


def write(fam: Family, data: dict) -> None:
    def sort(d):
        return {k: sort(v) for k, v in sorted(d.items())} if isinstance(d, dict) else d
    text = '# Written by `natemu-test lock`; do not edit (docs/testing.md).\n' + tomli_w.dumps(sort(data))
    path_of(fam).write_text(text)


def _repo_file(rel: str) -> pathlib.Path:
    return paths.repo_root() / rel


def fingerprint_input(fam: Family, name: str) -> str:
    d = fam.inputs[name]
    files = [_repo_file(d.generator)] if d.generator else []
    files += [_repo_file(s) for s in d.sources]
    return cache.key('input', name, *files, json.dumps(d.args))


def fingerprint_suite(fam: Family, suite: Suite, sources: list[pathlib.Path] = (), pin: str = '') -> str:
    guest = {k: v for k, v in sorted(suite.matrix.items()) if k.startswith('machine.')}
    inp = fingerprint_input(fam, suite.input) if suite.input else ''
    return cache.key('suite', suite.name, inp, json.dumps(guest), suite.reference or '', pin,
                     json.dumps(suite.extra.get('reference_overrides', {}), sort_keys=True), *sources)


def status(fam: Family, suite: Suite, sources: list[pathlib.Path] = (), pin: str = '') -> str:
    """'ok', 'missing' (never locked) or 'stale' (what decides the answer changed since)."""
    s = read(fam).get('suite', {}).get(suite.name)
    if not s: return 'missing'
    return 'ok' if s.get('fingerprint') == fingerprint_suite(fam, suite, sources, pin) else 'stale'


def check(fam: Family, sources_for=lambda s: ([], '')) -> list[str]:
    """Problems a reviewer cannot see in opaque hashes (R2.8): stale entries, answers collapsed by a normalizer that
    discards information, answers seeded from another tool than the suite declares."""
    lk, out = read(fam), []
    for suite in fam.suites:
        if suite.kind != 'deterministic': continue
        srcs, pin = sources_for(suite)
        st = status(fam, suite, srcs, pin)
        if st != 'ok': out.append(f'{fam.name}/{suite.name}: lock {st}'); continue
        s = lk['suite'][suite.name]
        answers = [a for a in s.get('cases', {}).values() if a]
        if len(answers) > 20 and len(set(answers)) < 0.1 * len(answers):
            out.append(f'{fam.name}/{suite.name}: {len(answers)} cases share {len(set(answers))} answers; '
                       'the normalizer may discard information')
        tool = s.get('seeded', {}).get('tool', '')
        if suite.reference and not tool.startswith(suite.reference):
            out.append(f'{fam.name}/{suite.name}: seeded by {tool or "nothing"}, declared reference {suite.reference}')
        missing = [c for c, a in s.get('cases', {}).items() if not a]
        if missing:
            out.append(f'{fam.name}/{suite.name}: {len(missing)} cases have no answer yet ({", ".join(missing[:5])})')
    return out
