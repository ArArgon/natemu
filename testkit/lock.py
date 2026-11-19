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


def import_closure(path: pathlib.Path, root: pathlib.Path) -> list[pathlib.Path]:
    """`path` and every Python file under `root` it imports, directly or through those files (any import statement,
    function-local ones included). A module is looked up as a generator finds it: next to the importing file (a script's
    own folder), then in `root`/tools (tools/reference's generators add it: raw_root), then in `root`; a package's
    `__init__.py` comes with it, and `from PKG import NAME` takes PKG/NAME.py when there is one. Names found nowhere under
    `root` (the standard library, installed packages) are left out."""
    import ast
    root = root.resolve()

    def find(base: pathlib.Path, dotted: str) -> list[pathlib.Path]:
        parts = dotted.split('.') if dotted else []
        for d in (base, root / 'tools', root):
            hit = []
            for i in range(1, len(parts) + 1):
                p = d.joinpath(*parts[:i])
                if p.with_suffix('.py').is_file() and i == len(parts): hit.append(p.with_suffix('.py'))
                elif (p / '__init__.py').is_file(): hit.append(p / '__init__.py')
                else: hit = []; break
            if hit: return hit
        return []

    seen, todo = set(), [path.resolve()]
    while todo:
        f = todo.pop()
        if f in seen or not f.is_file() or not f.is_relative_to(root): continue
        seen.add(f)
        try: tree = ast.parse(f.read_bytes(), str(f))
        except (SyntaxError, ValueError): continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names: todo += find(f.parent, a.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:              # relative: from the importing file's package
                    base = f.parent
                    for _ in range(node.level - 1): base = base.parent
                    mods = [base / (node.module or '').replace('.', '/')] if node.module else [base]
                    for m in mods:
                        todo += [m.with_suffix('.py')] if m.with_suffix('.py').is_file() else []
                        todo += [m / '__init__.py'] if (m / '__init__.py').is_file() else []
                        todo += [m / f'{a.name}.py' for a in node.names if (m / f'{a.name}.py').is_file()]
                    continue
                got = find(f.parent, node.module or '')
                todo += got
                if got and got[-1].name == '__init__.py':
                    todo += [got[-1].parent / f'{a.name}.py' for a in node.names
                             if (got[-1].parent / f'{a.name}.py').is_file()]
    return sorted(seen)


def input_cache_key(fam: Family, name: str) -> str:
    """The key of a built input in the cache: its fingerprint plus every repo file its generator imports
    (import_closure). The fingerprint itself, as locked, covers only the generator and its declared `sources`: adding
    the imports to it would make every locked suite stale. A corpus built from a changed import is caught when it is
    used, by the corpus hash locked with the answers (testkit.harness.programs)."""
    d = fam.inputs[name]
    root = paths.repo_root()
    closure = import_closure(_repo_file(d.generator), root) if d.generator else []
    return cache.key('input-built', fingerprint_input(fam, name),
                     *[x for f in closure for x in (f.relative_to(root.resolve()).as_posix(), f)])


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
