"""Known natemu bugs (owner, November 18): a test that catches one keeps running and reporting, but never blocks a run
or the gate, so bug fixes are never a dependency of test or feature work.

    tests/<family>/known-bugs.toml
    [bug.<id>]
    note = "what is wrong, and where it is recorded"
    cases = ["<suite>/<case>[*]", ...]          # patterns over the node id after `family.toml::` (sub-results included):
                                                # `*` and `?` are wildcards, everything else literal (brackets too)

A failing test a glob matches is reported as `known-bug` (grouped by bug; the failure's first line is kept in
junit.xml), not as a failure.
A matching test that passes is reported as `known-bug-passed`: the bug looks fixed, remove its entry. Neither is in
report.GATE_FAILS. A glob that matches nothing collected is not an error (the variant may not run on this machine).
"""
import functools
import pathlib
import re
import tomllib

FILE = 'known-bugs.toml'


def _pattern(g: str) -> re.Pattern:
    return re.compile(''.join('.*' if c == '*' else '.' if c == '?' else re.escape(c) for c in g) + r'\Z')


@functools.lru_cache(maxsize=None)
def _bugs(family_dir: str) -> tuple:
    f = pathlib.Path(family_dir) / FILE
    if not f.exists(): return ()
    d = tomllib.loads(f.read_text()).get('bug', {})
    return tuple((bid, b['note'], tuple(_pattern(g) for g in b['cases'])) for bid, b in d.items())


def match(nodeid: str, root: pathlib.Path) -> tuple[str, str] | None:
    """(bug id, note) for a node id one of its family's known bugs covers."""
    path, sep, rest = nodeid.partition('::')
    if not sep or not path.endswith('family.toml'): return None
    for bid, note, globs in _bugs(str(root / pathlib.Path(path).parent)):
        if any(g.match(rest) for g in globs): return bid, note
    return None


class Tracker:
    """Adjusts reports as the run shows them: a known bug's failure becomes `known-bug`, its pass `known-bug-passed`. A
    case's sub-results are reported before the case, so a case that failed only because known bugs among its
    sub-results failed (a runner's exit status) is `known-bug` too; one real sub-result failure keeps it failed."""
    def __init__(self, root: pathlib.Path):
        self.root = root
        self.subs = {}          # parent node id -> [known-bug failures, real failures]

    def adjust(self, rep):
        if rep.when != 'call' and not (rep.when == 'setup' and rep.failed): return rep
        parent = rep.nodeid.rsplit('::', 1)[0] if rep.nodeid.count('::') >= 2 else None
        m = match(rep.nodeid, self.root)
        if m is None and rep.failed and parent is None and self.subs.get(rep.nodeid, [0, 0]) [0] and \
                not self.subs[rep.nodeid][1]:
            m = ('known-bugs-only', 'every failed sub-result of this case is a known bug')
        if m is not None and not rep.skipped:
            bid, note = m
            if rep.failed:
                first = str(rep.longrepr).strip().splitlines()
                rep.outcome = 'skipped'
                rep.longrepr = (str(rep.location[0]), rep.location[1] or 0, f'{bid}: {note}')      # one summary line per bug
                rep.user_properties.append(('testkit', 'known-bug'))
                rep.user_properties.append(('testkit-failure', (first[0] if first else '')[:240]))  # kept in junit.xml
                if parent: self.subs.setdefault(parent, [0, 0])[0] += 1
                return rep
            rep.user_properties.append(('testkit', 'known-bug-passed'))
            rep.user_properties.append(('testkit-note', f'{bid} looks fixed: remove it from {FILE}'))
            return rep
        if rep.failed and parent: self.subs.setdefault(parent, [0, 0])[1] += 1
        return rep
