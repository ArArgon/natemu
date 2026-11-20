"""Unit suites (spec R3.8, R6.3a): Rust tests through cargo-nextest and the Python tool tests, each test reported as its
own result under the suite's item.

    [[suite]] harness = "units"  params = { tool = "nextest", fexc = true }
    [[suite]] harness = "units"  params = { tool = "nextest", filter = "package(x) | test(/y/)" }      # those tests only
    [[suite]] harness = "units"  params = { tool = "nextest", rest_of = ["SUITE", ...] }   # every test their filters leave
    [[suite]] harness = "units"  params = { tool = "pytest", include = [...], exclude = [...] }

A nextest suite runs as many test threads as it claims cores. `filter` and `rest_of` split one test list into suites
that run side by side (the slow disk tests on two cores, the rest on four) without a test falling between them: the rest
is computed from the other suites' own filters.
"""
import fnmatch
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from testkit import artifacts, paths
from testkit.harness import CaseResult, Requirement, SubResult


def python_test_files() -> list[str]:
    """The Python unit tests: what `unittest discover -s tools -p 'test_*.py'` loads (tools/ itself, and packages below
    it), and the test_*.py files of tests/lib and of the family folders, next to the scripts they test."""
    root = paths.repo_root()
    tools = root / 'tools'
    out = [p for p in tools.glob('test_*.py')]

    def walk(d):
        for sub in sorted(x for x in d.iterdir() if x.is_dir() and (x / '__init__.py').exists()):
            out.extend(sub.glob('test_*.py')); walk(sub)
    walk(tools)
    out += (root / 'tests').glob('*/test_*.py')
    return sorted(str(p.relative_to(root)) for p in out)


def parse_libtest_json(text: str) -> list[SubResult]:
    subs = []
    for line in text.splitlines():
        try: e = json.loads(line)
        except ValueError: continue
        if e.get('type') != 'test' or e.get('event') not in ('ok', 'failed', 'ignored'): continue
        outcome = {'ok': 'passed', 'failed': 'failed', 'ignored': 'skipped'}[e['event']]
        name = e['name'].replace('$', '::')
        subs.append(SubResult(name, outcome, (e.get('stdout') or '')[-4000:] if outcome == 'failed' else '',
                              float(e.get('exec_time') or 0)))
    return subs


def parse_junit(path: pathlib.Path) -> list[SubResult]:
    subs = []
    for tc in ET.parse(path).iter('testcase'):
        name = f"{tc.get('classname')}::{tc.get('name')}"
        bad = tc.find('failure') if tc.find('failure') is not None else tc.find('error')
        outcome = 'failed' if bad is not None else 'skipped' if tc.find('skipped') is not None else 'passed'
        text = (bad.get('message', '') + '\n' + (bad.text or '')) if bad is not None else ''
        subs.append(SubResult(name, outcome, text[-4000:], float(tc.get('time') or 0)))
    return subs


class Units:
    def cases(self, fam, suite):
        if suite.params.get('tool') == 'nextest':
            self.nextest_filter(fam, suite)          # a misconfigured filter fails collection, not the run
            return ['all']
        inc, exc = suite.params.get('include'), suite.params.get('exclude', [])
        files = python_test_files()
        if inc: files = [f for f in files if any(fnmatch.fnmatch(f, g) for g in inc)]
        return [f for f in files if not any(fnmatch.fnmatch(f, g) for g in exc)]

    def requirements(self, fam, suite, variant):
        need = [Requirement('artifact', 'qemu-system')]
        need += [Requirement('artifact', 'nextest')] if suite.params.get('tool') == 'nextest' else []
        return need + ([Requirement('artifact', 'fexc')] if suite.params.get('fexc') else [])

    def resolve(self, req):
        return artifacts.resolve(req.name)

    def run(self, batch, config):
        return {it.nodeid: (self.nextest(it) if it.suite.params.get('tool') == 'nextest' else self.pytest_file(it)) for it in batch}

    def _env(self, it):
        qbin = artifacts.qemu_system()                 # the pinned qemu-img first on PATH
        env = dict(os.environ, NATEMU_REQUIRE_QEMU_IMG='1', NATEMU_QEMU_IMG=str(qbin / 'qemu-img'),
                   PATH=f'{qbin}:{os.environ.get("PATH", "")}')
        if it.suite.params.get('fexc'): env['FEXC'] = str(artifacts.fexc())
        return env

    @staticmethod
    def nextest_filter(fam, suite) -> list[str]:
        """The `-E` argument of a nextest suite's `filter` or `rest_of` (nextest ORs several -E, so this is one). A
        mistake in them is a configuration error, raised when the family is collected (`cases`)."""
        p = suite.params
        parts = [f'({p["filter"]})'] if p.get('filter') else []
        for n in p.get('rest_of', []):
            other = next((s for s in fam.suites if s.name == n), None)
            if other is None or not other.params.get('filter'):
                raise ValueError(f'{fam.name}/{suite.name}: rest_of names {n!r}, which is not a suite of this family with a filter')
            parts.append(f'not ({other.params["filter"]})')
        if parts and '-E' in p.get('args', []):
            raise ValueError(f'{fam.name}/{suite.name}: -E in args beside filter/rest_of would widen the filter (nextest ORs them)')
        return ['-E', ' & '.join(parts)] if parts else []

    def nextest(self, it) -> CaseResult:
        env = self._env(it)
        env['PATH'] = f'{artifacts.nextest().parent}:{env["PATH"]}'
        env['NEXTEST_EXPERIMENTAL_LIBTEST_JSON'] = '1'
        cores = it.suite.needs.cores
        threads = ['--test-threads', str(cores)] if isinstance(cores, int) else []     # "all": nextest's default
        r = subprocess.run(['cargo', '+1.97.0', 'nextest', 'run', '--offline', '--locked', '--no-fail-fast',
                            '--message-format', 'libtest-json', *threads, *self.nextest_filter(it.fam, it.suite),
                            *it.suite.params.get('args', [])],
                           cwd=paths.repo_root(), env=env, capture_output=True, text=True)
        subs = parse_libtest_json(r.stdout)
        if not subs:
            return CaseResult('failed', f'nextest ran no tests (exit {r.returncode}):\n{r.stderr[-3000:]}')
        bad = [s.name for s in subs if s.outcome == 'failed']
        if bad: return CaseResult('failed', f'{len(bad)} failed: ' + ', '.join(bad[:10]), sub=subs)
        if r.returncode != 0: return CaseResult('failed', f'nextest exit {r.returncode}:\n{r.stderr[-3000:]}', sub=subs)
        return CaseResult('passed', sub=subs)

    def pytest_file(self, it) -> CaseResult:
        with tempfile.TemporaryDirectory() as d:
            xml = pathlib.Path(d) / 'j.xml'
            r = subprocess.run([sys.executable, '-m', 'pytest', '-c', '/dev/null', '-p', 'no:cacheprovider', '-q',
                                '--rootdir', str(paths.repo_root()), f'--junitxml={xml}', it.case],
                               cwd=paths.repo_root(), env=self._env(it), capture_output=True, text=True)
            subs = parse_junit(xml) if xml.exists() else []
        if not subs:
            return CaseResult('failed', f'pytest ran nothing (exit {r.returncode}):\n{(r.stdout + r.stderr)[-3000:]}')
        bad = [s.name for s in subs if s.outcome == 'failed']
        return CaseResult('failed' if bad or r.returncode not in (0, 5) else 'passed',
                          f'{len(bad)} failed: ' + ', '.join(bad[:10]) if bad else '', sub=subs)


HARNESS = Units()
