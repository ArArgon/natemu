"""Collect `family.toml` files into pytest items: one CaseItem per (suite, case, variant). Exclusions, placement and
lock state are decided here, so an item that will not run says why before anything starts (spec R2.13, R5.4)."""
import pathlib

import pytest

from testkit import family, host
from testkit.harness import CaseResult

KEY = pytest.StashKey[dict]()       # session stash: nodeid -> CaseResult, filled a batch at a time


class FamilyFile(pytest.File):
    def collect(self):
        fam = family.load(pathlib.Path(self.path))
        h = host.detect()
        require_real = frozenset(self.config.getoption('require_real') or ())
        for suite in fam.suites:
            harness = family.harness_for(fam, suite)
            status = harness.lock_status(fam, suite) if hasattr(harness, 'lock_status') else 'ok'
            cases = harness.cases(fam, suite)
            if status != 'ok' and not cases: cases = ['(unlocked)']
            locked_ex = harness.locked_exclusions(fam, suite) if status == 'ok' and hasattr(harness, 'locked_exclusions') else None
            for v in family.expand(suite):
                place = family.placement(v, h, require_real)
                if locked_ex is not None and fam.rules:
                    met = {k[5:]: ('real' if v.get(k) else 'hidden') for k, _ in v.params if k.startswith('host.')}
                    now = sorted(c for c in cases if fam.rules(suite.name, c, v, met))
                    if now != sorted(locked_ex.get(v.id, [])):
                        yield ExclusionCheck.from_parent(self, name=f'{suite.name}/(exclusions)[{v.id}]',
                                                         why=f'{len(now)} excluded now, {len(locked_ex.get(v.id, []))} in the lock')
                for case in cases:
                    name = f'{suite.name}/{case}' + (f'[{v.id}]' if v.params else '')
                    yield CaseItem.from_parent(self, name=name, fam=fam, suite=suite, case=case, variant=v,
                                               place=place, harness=harness, lock_status=status)


class CaseItem(pytest.Item):
    def __init__(self, *, fam, suite, case, variant, place, harness, lock_status, **kw):
        super().__init__(**kw)
        self.fam, self.suite, self.case, self.variant, self.place, self.harness = fam, suite, case, variant, place, harness
        self.unit_key = (fam.name, suite.name, variant.id) + ((case,) if suite.kind == 'legacy' else ())
        if suite.smoke is True or (isinstance(suite.smoke, list) and case in suite.smoke) or \
                (hasattr(harness, 'is_smoke') and harness.is_smoke(case)) or \
                suite.extra.get('cases', {}).get(case, {}).get('smoke') is True:
            self.add_marker('smoke')
        self.add_marker(suite.kind)
        skip = None
        if not place.runnable:
            skip = ('not-runnable', place.reason)
        elif lock_status != 'ok':
            skip = ('stale-lock', f'{suite.name}: lock {lock_status} (run natemu-test lock {fam.dir.name})')
        elif fam.rules and (why := fam.rules(suite.name, case, variant, dict(place.met))):
            skip = ('excluded', why)
        self.preset = skip
        if skip:
            self.user_properties.append(('testkit', skip[0]))
            self.add_marker(pytest.mark.skip(reason=f'{skip[0]}: {skip[1]}'))

    def runtest(self):
        results = self.session.stash.setdefault(KEY, {})
        if self.nodeid not in results:
            batch = [i for i in self.session.items if isinstance(i, CaseItem) and i.unit_key == self.unit_key
                     and not i.preset and i.nodeid not in results]
            results.update(self.harness.run(batch, self.config))
        r: CaseResult = results.get(self.nodeid) or CaseResult('failed', 'the harness reported nothing for this case')
        for s in r.sub:
            emit_sub(self, s)
        if r.category: self.user_properties.append(('testkit', r.category))
        if getattr(r, 'own', False): self.user_properties.append(('testkit-own-failure', '1'))
        if r.category == 'measured': self.user_properties.append(('testkit-note', r.message))
        if r.outcome == 'skipped': pytest.skip(f'{r.category or "skipped"}: {r.message}')
        if r.outcome == 'failed': raise CaseFailure(r.message)

    def repr_failure(self, excinfo):
        if isinstance(excinfo.value, CaseFailure): return str(excinfo.value)
        # pytest's own repr lists each frame's arguments; under subprocess.run(..., env=...) that is the environment
        # (tokens), so a harness error is shown without them, and without locals even under --showlocals
        style = self.config.getoption('tbstyle', 'auto')
        return excinfo.getrepr(funcargs=False, showlocals=False, style='long' if style == 'auto' else style,
                               tbfilter=True)

    def reportinfo(self):
        return self.path, 0, self.name


class ExclusionCheck(pytest.Item):
    """The family's rule excludes other cases than the lock records (R2.13): a gate failure, never a quiet pass."""
    def __init__(self, *, why, **kw):
        super().__init__(**kw)
        self.user_properties.append(('testkit', 'exclusion-changed'))
        self.add_marker(pytest.mark.skip(reason=f'exclusion-changed: {why} (re-lock if intended)'))

    def runtest(self): pass

    def reportinfo(self): return self.path, 0, self.name


class CaseFailure(Exception):
    pass


def emit_sub(item, s):
    """A per-test result of a units or campaign suite, reported under its own node id (with its category, if any)."""
    from _pytest.reports import TestReport
    nodeid = f'{item.nodeid}::{s.name}'
    longrepr = None if s.outcome == 'passed' else ((str(item.path), 0, s.longrepr or s.outcome) if s.outcome == 'skipped'
                                                   else s.longrepr or 'failed')
    cat = getattr(s, 'category', None)
    props = ([('testkit', cat)] if cat else []) + ([('testkit-note', s.longrepr)] if s.longrepr and (cat or s.outcome == 'passed') else [])
    rep = TestReport(nodeid, (str(item.path), None, nodeid), {}, s.outcome, longrepr, 'call', duration=s.duration,
                     user_properties=props)
    item.ihook.pytest_runtest_logreport(report=rep)
