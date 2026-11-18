"""The pytest plugin: options, the family.toml collector, result categories and the gate verdict. The run loop
(testkit.runloop) and selection (testkit.select) register their own hooks from here."""
import pathlib

import pytest

from testkit import report


def pytest_addoption(parser):
    g = parser.getgroup('testkit')
    g.addoption('--tier', default=None, choices=['smoke', 'affected', 'gate', 'campaign', 'bench'])
    g.addoption('--gate', action='store_true', help='the merge gate: every category in report.GATE_FAILS fails the run')
    g.addoption('--base', default=None, help='base revision for --tier affected (default: merge-base with main)')
    g.addoption('--require-real', action='append', default=[], help='a host feature that must really be absent or present')
    g.addoption('--fresh', action='store_true', help='never reuse an earlier result')
    g.addoption('--priority', default=None, choices=['interactive', 'gate'])
    g.addoption('--diff', action='store_true', help='on a failure, seed the one case to show what differs')
    g.addoption('--testkit-worker', default=None, help=argparse_hidden())
    g.addoption('--testkit-report', default=None, help=argparse_hidden())
    g.addoption('--testkit-inprocess', action='store_true', help='run items in this process (no run loop)')


def argparse_hidden():
    import argparse
    return argparse.SUPPRESS


def pytest_configure(config):
    config.pluginmanager.register(Summary(), 'testkit-summary')
    import importlib
    config.pluginmanager.register(importlib.import_module('testkit.runloop'), 'testkit-runloop')
    for m in ('smoke: a smoke case', 'deterministic: locked answer', 'judged: judged by code', 'legacy: wrapped legacy step',
              'units: unit test suite'):
        config.addinivalue_line('markers', m)


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

    def pytest_runtest_logreport(self, report):
        from testkit import report as rep
        c = rep.category(report)
        if c and (report.when == 'call' or (report.when == 'setup' and report.skipped)):
            reason = report.longrepr[2] if report.skipped and isinstance(report.longrepr, tuple) else ''
            self.items.append((c, report.nodeid, reason))

    def pytest_terminal_summary(self, terminalreporter):
        groups = {}
        for c, nodeid, reason in self.items:
            if c in ('reused', 'wrapped'): continue
            groups.setdefault((c, reason.removeprefix('Skipped: ')), []).append(nodeid)
        if not groups: return
        terminalreporter.section('testkit')
        for (c, reason), ids in sorted(groups.items()):
            terminalreporter.write_line(f'{c} ({len(ids)}): {reason}')
            for i in ids[:5]: terminalreporter.write_line(f'    {i}')
            if len(ids) > 5: terminalreporter.write_line(f'    ... and {len(ids) - 5} more')


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if not session.config.getoption('gate', False) or session.config.getoption('testkit_worker'): return
    tr = session.config.pluginmanager.get_plugin('terminalreporter')
    stats = tr.stats if tr else {}
    ok, bad = report.gate_verdict(stats)
    if not session.items:
        ok, bad = False, ['nothing selected']
    if not ok:
        session.exitstatus = 1
        if tr: tr.write_line('gate: not passed: ' + ', '.join(bad), red=True)
