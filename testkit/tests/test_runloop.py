import itertools
import json
import os
import subprocess
import sys
import time

import pytest

from testkit import paths


@pytest.fixture
def env(monkeypatch, tmp_path, pytester, family_dir):
    family_dir('sleepfam')
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'cache'))
    monkeypatch.setenv('NATEMU_TEST_CORES', '4,0')
    monkeypatch.setenv('NATEMU_TEST_WINDOW', '600,0')
    monkeypatch.setenv('TESTKIT_RUN_DIR', str(tmp_path / 'run'))
    monkeypatch.setenv('SLEEP_LOG', str(tmp_path / 'sleep.log'))
    return tmp_path


def test_reports_replayed(pytester, env):
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '-k', 'shared', f'--junitxml={env / "j.xml"}', 'tests')
    assert '6 passed' in r.stdout.str(), r.stdout.str()
    assert (env / 'j.xml').read_text().count('<testcase') == 6
    assert len((env / 'run' / 'units').glob('*.jsonl').__iter__().__next__().read_text().splitlines()) > 0


def test_worker_killed_items_error(pytester, env):
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '-k', 'die or shared', 'tests')
    out = r.stdout.str()
    assert r.ret == 1 and '6 passed' in out and '2 failed' in out, out
    assert 'worker died (signal 9)' in out


def test_unpreparable_resource(pytester, env):
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '-k', 'needsbad or shared', 'tests')
    out = r.stdout.str()
    assert '6 passed' in out and '1 unpreparable' in out and 'connection refused' in out, out


def test_two_runs_share_machine(pytester, env):
    procs = []
    for tag in ('a', 'b'):
        e = dict(os.environ, RUN_TAG=tag, TESTKIT_RUN_DIR=str(env / f'run-{tag}'))
        procs.append(subprocess.Popen([sys.executable, '-m', 'pytest', '-p', 'testkit.plugin', '-p', 'no:cacheprovider',
                                       '-q', '-k', 'shared or excl', 'tests'], cwd=pytester.path, env=e,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
    outs = [p.communicate(60)[0].decode() for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    iv = [json.loads(l) for l in (env / 'sleep.log').read_text().splitlines()]
    assert len(iv) == 16 and {x[4] for x in iv} == {'a', 'b'}
    for a, b in itertools.combinations(iv, 2):
        if a[0] < b[1] and b[0] < a[1]: assert not a[2] and not b[2], (a, b)
    for x in iv:
        mid = (x[0] + x[1]) / 2
        assert sum(y[3] for y in iv if y[0] <= mid < y[1]) <= 4


def test_submit_wait_status(env, monkeypatch, tmp_path):
    monkeypatch.delenv('TESTKIT_RUN_DIR')
    fam = paths.repo_root() / 'testkit' / 'tests' / 'fixtures' / 'sleepfam' / 'family.toml'
    e = dict(os.environ, PYTHONPATH=str(paths.repo_root()))
    out = subprocess.run([sys.executable, '-m', 'testkit', 'submit', '-p', 'no:cacheprovider', '-k', 'shared', str(fam)],
                         capture_output=True, text=True, env=e, cwd=paths.repo_root())
    rid = out.stdout.strip()
    assert out.returncode == 0 and len(rid) == 20, out
    st = subprocess.run([sys.executable, '-m', 'testkit', 'status'], capture_output=True, text=True, env=e)
    assert 'machine:' in st.stdout
    w = subprocess.run([sys.executable, '-m', 'testkit', 'wait', rid], capture_output=True, text=True, env=e, timeout=60)
    assert w.returncode == 0 and 'exit status 0' in w.stdout, w.stdout + w.stderr


def test_prepare_only_runs_nothing(pytester, env):
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-prepare-only', '-k', 'shared', 'tests')
    assert r.ret == 0 and 'nothing run' in r.stdout.str(), r.stdout.str()
    assert not (env / 'sleep.log').exists()                      # no test body ran
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-prepare-only', '-k', 'needsbad', 'tests')
    out = r.stdout.str()
    assert r.ret == 1 and '1 unpreparable' in out and 'connection refused' in out, out


def test_a_unit_with_unrecorded_items_is_estimated_from_its_suite(monkeypatch, tmp_path):
    """Units must have an expected length to backfill a draining machine: an item with no recorded duration takes the
    mean of its suite's recorded items (9 of 12 cores sat idle while a window drained, every gate unit unknown)."""
    from types import SimpleNamespace as NS
    from testkit import runloop
    from testkit.sched.policy import Config
    suite = NS(name='s', batch=2, kind='runner', params={})
    fam, var = NS(name='f'), NS(id='', params={})
    items = [NS(nodeid=f'n{i}', unit_key=('f', 's', ''), suite=suite, fam=fam, variant=var, case=f'c{i}',
                harness=object(), place=NS(env={})) for i in range(4)]
    other = NS(nodeid='m0', unit_key=('f', 't', ''), suite=NS(name='t', batch=1, kind='runner', params={}), fam=fam,
               variant=var, case='m0', harness=object(), place=NS(env={}))
    monkeypatch.setattr(runloop.durations, 'load', lambda: {'n0': 10.0, 'n1': 20.0})
    monkeypatch.setattr(runloop, 'run_dir', lambda c: tmp_path)
    monkeypatch.setattr(runloop, 'it_needs', lambda it: NS(cores=1, exclusive=False, perf=False, dedicated=False))
    pool = NS(cfg=Config(p_cores=8, e_cores=4), unsatisfiable=lambda r: False)
    config = NS(getoption=lambda k: None)
    units, _ = runloop.build_units(items + [other], config, pool)
    got = sorted(u.req.expected for u in units if u.req.expected is not None)
    assert got == [30.0, 30.0]                          # (10 + 20) and (15 + 15): the unrecorded items take the mean
    assert [u.req.expected for u in units if u.items[0].suite.name == "t"] == [None]   # nothing recorded: still unknown
