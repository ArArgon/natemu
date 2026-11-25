"""The pytest plugin: options, the family.toml collector, result categories and the gate verdict. The run loop
(testkit.runloop) and selection (testkit.select) register their own hooks from here."""
import functools
import os
import pathlib

import pytest

from testkit import report


def pytest_addoption(parser):
    g = parser.getgroup('testkit')
    g.addoption('--tier', default=None, choices=['smoke', 'affected', 'gate', 'campaign', 'bench', 'debug'])
    g.addoption('--gate', action='store_true', help='the merge gate: every category in report.GATE_FAILS fails the run')
    g.addoption('--accept-not-runnable', action='store_true',
                help='a person decided to pass this gate without the tests this machine cannot run (risk: high)')
    g.addoption('--base', default=None, help='base revision for --tier affected (default: merge-base with main)')
    g.addoption('--require-real', action='append', default=[], help='a host feature that must really be absent or present')
    g.addoption('--fresh', action='store_true', help='never reuse an earlier result')
    g.addoption('--priority', default=None, choices=['interactive', 'gate'])
    g.addoption('--diff', action='store_true', help='on a failure, seed the one case to show what differs')
    g.addoption('--keep-outputs', action='store_true', help="keep passed cases' output folders in the run folder")
    g.addoption('--testkit-worker', default=None, help=argparse_hidden())
    g.addoption('--testkit-report', default=None, help=argparse_hidden())
    g.addoption('--testkit-inprocess', action='store_true', help='run items in this process (no run loop)')
    g.addoption('--testkit-prepare-only', action='store_true', help='prepare what the selection needs; run nothing')
    g.addoption('--complete', default=None, metavar='RUN', help='with --gate: run only what the gate in run folder RUN (this commit, a '
                'less capable machine) could not run; the verdict is that run and this one together')
    g.addoption('--with-run', action='append', default=[], help='another run folder of the same commit whose passes '
                'cover items not runnable here (R5.7)')


def argparse_hidden():
    import argparse
    return argparse.SUPPRESS


def pytest_configure(config):
    if config.getoption('keep_outputs'):
        import os
        os.environ['TESTKIT_KEEP_OUTPUTS'] = '1'       # workers inherit it (testkit.harness.runner.drop_outputs)
    config.pluginmanager.register(Summary(), 'testkit-summary')
    import importlib
    config.pluginmanager.register(importlib.import_module('testkit.runloop'), 'testkit-runloop')
    config.pluginmanager.register(importlib.import_module('testkit.select'), 'testkit-select')
    for m in ('smoke: a smoke case', 'deterministic: locked answer', 'judged: judged by code', 'bench: a benchmark',
              'units: unit test suite'):
        config.addinivalue_line('markers', m)


def pytest_ignore_collect(collection_path: pathlib.Path, config):
    """tests/ holds family.toml files and the scripts they run; its test_*.py files are unit tests the unit family runs in
    its own pytest (testkit.harness.units), never collected here as plain pytest modules."""
    if collection_path.suffix == '.py' and collection_path.name.startswith('test_'):
        from testkit import paths
        if collection_path.resolve().is_relative_to(paths.repo_root() / 'tests'): return True
    return None


def pytest_collect_file(parent, file_path: pathlib.Path):
    if file_path.name == 'family.toml':
        from testkit.collect import FamilyFile
        return FamilyFile.from_parent(parent, path=file_path)


def pytest_report_teststatus(report: pytest.TestReport, config):
    from testkit import report as rep
    return rep.teststatus(report)


class Summary:
    """Collects every item that did not simply pass or fail, and prints them grouped by category and reason, with
    counts (spec R2.13, R5.4)."""
    def __init__(self):
        self.items = []
        self.outcome = {}           # nodeid -> category or pytest outcome (coverage.json)

    def pytest_runtest_logreport(self, report):
        from testkit import report as rep
        c = rep.category(report)
        if report.when == 'call' or (report.when == 'setup' and not report.passed) or (report.when == 'teardown' and report.failed):
            self.outcome[report.nodeid] = c if c else report.outcome
        if c and (report.when == 'call' or (report.when == 'setup' and report.skipped)):
            reason = report.longrepr[2] if report.skipped and isinstance(report.longrepr, tuple) else \
                dict(report.user_properties).get('testkit-note', '')
            self.items.append((c, report.nodeid, reason))

    def pytest_terminal_summary(self, terminalreporter):
        t = getattr(terminalreporter.config, '_testkit_timings', None)
        if t and t.get('collected'):
            import time
            from testkit import timings
            terminalreporter.section('times')
            try: lines = timings.brief(dict(t, pytest_end=time.time()))
            except Exception as e: lines = [f'could not summarize the times: {e!r}']
            for line in lines:
                terminalreporter.write_line(line)
            terminalreporter.write_line('  (cleanup and per-unit detail: tools/natemu-test times %s --detail)' % t['run'])
        groups = {}
        for c, nodeid, reason in self.items:
            if c == 'reused': continue
            if c == 'measured': reason = nodeid.rsplit('/', 1)[-1] + ': ' + reason
            groups.setdefault((c, reason.removeprefix('Skipped: ')), []).append(nodeid)
        if not groups: return
        terminalreporter.section('testkit')
        for (c, reason), ids in sorted(groups.items()):
            terminalreporter.write_line(f'{c} ({len(ids)}): {reason}')
            for i in ids[:5]: terminalreporter.write_line(f'    {i}')
            if len(ids) > 5: terminalreporter.write_line(f'    ... and {len(ids) - 5} more')


@functools.cache
def _commit():
    """The tested commit (plus a hash of uncommitted changes), read once when the session starts (pytest_sessionstart): a
    commit made while a long run goes must not become the commit the run says it tested."""
    import subprocess
    from testkit import paths
    import hashlib
    r = lambda *a: subprocess.run(['git', '-C', str(paths.repo_root()), *a], capture_output=True, text=True).stdout
    diff = r('diff', 'HEAD')
    return r('rev-parse', 'HEAD').strip() + (('+' + hashlib.sha256(diff.encode()).hexdigest()[:12]) if diff else '')


def write_coverage(session):
    import json, os
    from testkit import host
    d = os.environ.get('TESTKIT_RUN_DIR')
    s = session.config.pluginmanager.get_plugin('testkit-summary')
    if not d or s is None: return
    h = host.detect()
    (pathlib.Path(d) / 'coverage.json').write_text(json.dumps(
        {'commit': _commit(), 'machine': h.machine_id, 'chip': h.chip, 'items': s.outcome}, indent=0))


def accepted_without(session, tr, missing) -> bool:
    """A gate in which everything that ran passed, but this machine could not run some tests (too few cores, a missing
    feature) and no other run covers them (--with-run): it is incomplete, not passed. Nothing is asked here (a prompt
    would block a run nobody watches): the report names the three ways to close it with their risk, for whoever ran it
    (an agent hands them to the user), and each is a later command. True only with --accept-not-runnable, which records
    a person's decision to ignore them."""
    names = sorted({r.nodeid for r in missing})
    took = session.config.getoption('accept_not_runnable', False)
    if tr:
        run = os.environ.get('TESTKIT_RUN_DIR') or '.natemu/runs/<this run>'
        tr.write_sep('!', f'ALL TESTS PASSED, BUT {len(names)} TESTS DID NOT RUN ON THIS MACHINE', red=True, bold=True)
        for n in names[:40]: tr.write_line(f'  {n}', red=True)
        if len(names) > 40: tr.write_line(f'  ... and {len(names) - 40} more', red=True)
        if took:
            tr.write_line('gate: passed WITHOUT them (--accept-not-runnable: option 1, risk: HIGH)', red=True, bold=True)
            return True
        tr.write_line('The gate is not complete. The user chooses how to close it (an agent: tell the user, do not choose):', red=True, bold=True)
        tr.write_line('  1. Ignore these tests                          [risk: HIGH]     nothing has checked them on this commit', red=True)
        tr.write_line(f'       here, runs nothing again: tools/natemu-test run --gate --complete {run} --accept-not-runnable', red=True)
        tr.write_line('  2. Run only these on a machine that can        [risk: med-low]  the rest is this machine\'s result', yellow=True)
        tr.write_line(f'       there, same commit: tools/natemu-test run --gate --complete {run}   (copy the run folder over)', yellow=True)
        tr.write_line('  3. Run the whole gate again on that machine    [risk: low]', green=True)
        tr.write_line('       there: tools/natemu-test run --gate', green=True)
    return took


def run_to_complete(config) -> dict | None:
    """--complete RUN: that run's recorded outcomes {nodeid: outcome}; it must be of this commit (and the same uncommitted changes)."""
    import json
    d = config.getoption('complete', None)
    if not d: return None
    f = pathlib.Path(d) / 'coverage.json'
    if not f.exists(): raise pytest.UsageError(f'--complete: no coverage.json in {d} (a finished run folder, .natemu/runs/<id>)')
    c = json.loads(f.read_text())
    if c['commit'] != _commit(): raise pytest.UsageError(f"--complete: {d} tested {c['commit']}, this checkout is {_commit()}")
    return c['items']


def covered_elsewhere(session) -> set:
    import json
    out, commit = set(), _commit()
    for d in session.config.getoption('with_run') or ():
        f = pathlib.Path(d) / 'coverage.json'
        if not f.exists(): continue
        c = json.loads(f.read_text())
        if c['commit'] != commit: continue          # same commit and the same uncommitted changes only
        out |= {n for n, o in c['items'].items() if o in ('passed', 'reused')}
    return out


def pytest_sessionstart(session):
    _commit()


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if session.config.getoption('testkit_worker'): return
    write_coverage(session)
    tr = session.config.pluginmanager.get_plugin('terminalreporter')
    stats = dict(tr.stats) if tr else {}
    if not session.config.getoption('gate', False):
        if (bad := report.run_failures(stats)) and session.exitstatus == 0:
            session.exitstatus = 1
            if tr: tr.write_line('testkit: not everything could run: ' + ', '.join(bad), red=True)
        return
    if stats.get('not-runnable'):
        elsewhere = covered_elsewhere(session)
        stats['not-runnable'] = [r for r in stats['not-runnable'] if r.nodeid not in elsewhere]
    ok, bad = report.gate_verdict(stats)
    if first := run_to_complete(session.config):       # the other machine's gate counts as it was, except what this run ran for it
        theirs = [o for o in first.values() if o in report.GATE_FAILS]
        if theirs: ok, bad = False, bad + [f'{len(theirs)} not passed in the run being completed']
    if ok and session.items and stats.get('not-runnable') and not accepted_without(session, tr, stats['not-runnable']):
        ok, bad = False, [f"incomplete: {len(stats['not-runnable'])} tests did not run on this machine (options above)"]
    if not session.items:
        ok, bad = False, ['nothing selected']
    if not ok:
        session.exitstatus = 1
        if tr: tr.write_line('gate: not passed: ' + ', '.join(bad), red=True)
