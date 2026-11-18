"""Which tests a run selects (spec R6): tiers, and change-based selection by declared path rules (R6.3b).

    smoke     unit suites, plus every family's smoke cases
    affected  smoke, plus every family whose `covers` rules match a changed file (the default of `natemu-test run`)
    gate      everything except campaign and bench suites
    campaign, bench   only suites declaring that tier (`tier = "campaign"`)

Unit suites run in every tier (R6.3a). A changed file matching a `shared` rule of tests/selection.toml selects every
family's smoke cases. A changed file no rule claims is listed, never guessed at.
"""
import functools
import re
import subprocess
import tomllib

import pytest

from testkit import paths
from testkit.collect import CaseItem


@functools.cache
def _rx(glob: str):
    out, i = '', 0
    while i < len(glob):
        if glob.startswith('**', i): out += '.*'; i += 2
        elif glob[i] == '*': out += '[^/]*'; i += 1
        elif glob[i] == '?': out += '[^/]'; i += 1
        else: out += re.escape(glob[i]); i += 1
    return re.compile(out + '(/.*)?$')          # a rule naming a folder covers everything in it


def matches(path: str, globs) -> bool:
    return any(_rx(g).match(path) for g in globs)


def changed_files(base: str | None = None) -> list[str]:
    root = str(paths.repo_root())

    def git(*a):
        return subprocess.run(['git', '-C', root, *a], capture_output=True, text=True).stdout.split('\n')
    if base is None:
        mb = git('merge-base', 'HEAD', 'main')[0]
        base = mb or 'HEAD'
    files = set(git('diff', '--name-only', base)) | set(git('ls-files', '--others', '--exclude-standard'))
    return sorted(f for f in files if f)


def shared_rules() -> list[str]:
    f = paths.repo_root() / 'tests' / 'selection.toml'
    return tomllib.loads(f.read_text()).get('shared', []) if f.exists() else []


def affected(families, changed: list[str], shared=None) -> tuple[set, list[str], bool]:
    """(families selected, unmapped files, whether shared machinery changed)."""
    shared = shared_rules() if shared is None else shared
    sel, unmapped, any_shared = set(), [], False
    for f in changed:
        hit = {fam.name for fam in families if matches(f, fam.covers)}
        if matches(f, shared): any_shared = True; hit.add('*')
        if not hit: unmapped.append(f)
        sel |= hit - {'*'}
    return sel, unmapped, any_shared


def keep(item: CaseItem, tier: str, chosen: set) -> bool:
    t = item.suite.extra.get('tier')
    if tier in ('campaign', 'bench'): return t == tier
    if t in ('campaign', 'bench'): return False
    if item.suite.kind == 'units' or tier == 'gate': return True
    if item.get_closest_marker('smoke'): return True
    return tier == 'affected' and item.fam.name in chosen


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    tier = config.getoption('tier') or ('gate' if config.getoption('gate') else None)
    if tier is None or config.getoption('testkit_worker'): return
    chosen = set()
    tr = config.pluginmanager.get_plugin('terminalreporter')
    if tier == 'affected':
        fams = {i.fam.name: i.fam for i in items if isinstance(i, CaseItem)}.values()
        changed = changed_files(config.getoption('base'))
        chosen, unmapped, _ = affected(list(fams), changed)
        if tr:
            tr.write_line(f'testkit: {len(changed)} changed file(s); families selected: {", ".join(sorted(chosen)) or "none"}')
            if unmapped:
                tr.write_line(f'testkit: {len(unmapped)} changed file(s) no family covers (widen with --tier gate or name them):')
                for f in unmapped[:20]: tr.write_line(f'    {f}')
    kept = [i for i in items if not isinstance(i, CaseItem) or keep(i, tier, chosen)]
    dropped = [i for i in items if i not in kept]
    if dropped: config.hook.pytest_deselected(items=dropped)
    items[:] = kept
    if not kept and tr:
        tr.write_line(f'testkit: nothing selected for tier {tier}', yellow=True)
