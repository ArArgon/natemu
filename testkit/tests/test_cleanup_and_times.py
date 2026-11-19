import json
import os
import time

from testkit import cache, clean, paths, timings
from testkit.harness import runner


def test_drop_outputs_removes_a_passed_case_and_its_first_attempt(tmp_path, monkeypatch):
    o = tmp_path / 'out' / 'c'; (o / 'x').mkdir(parents=True); (tmp_path / 'out' / 'c.first').mkdir()
    monkeypatch.delenv('TESTKIT_KEEP_OUTPUTS', raising=False)
    runner.drop_outputs(o)
    assert not o.exists() and not (tmp_path / 'out' / 'c.first').exists()
    (o / 'x').mkdir(parents=True); monkeypatch.setenv('TESTKIT_KEEP_OUTPUTS', '1')
    runner.drop_outputs(o)
    assert o.exists()


def _run(root, name, finished=True, keep=False, pid=None):
    d = paths.runs_dir(root) / name; d.mkdir(parents=True)
    (d / 'run.json').write_text(json.dumps({'id': name, 'pid': pid or 999999, 'started': 0}))
    if finished: (d / 'result.json').write_text('{"exit": 0}')
    if keep: (d / 'keep').write_text('')
    (d / 'big').write_bytes(b'x' * 10000)
    return d


def test_clean_keeps_running_and_kept_runs_and_purge_empties_the_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'cache'))
    monkeypatch.setattr(paths, 'repo_root', lambda: tmp_path / 'wt')
    (tmp_path / 'wt' / 'testkit').mkdir(parents=True); (tmp_path / 'wt' / 'testkit' / 'requirements.lock').write_text('x')
    monkeypatch.setattr(clean.tempfile, 'gettempdir', lambda: str(tmp_path / 'tmp'))
    (tmp_path / 'tmp' / 'testkit-seed-abc').mkdir(parents=True); (tmp_path / 'tmp' / 'other').mkdir()
    done, kept = _run(tmp_path / 'wt', 'a'), _run(tmp_path / 'wt', 'b', keep=True)
    live = _run(tmp_path / 'wt', 'c', finished=False, pid=os.getpid())
    crashed = _run(tmp_path / 'wt', 'd', finished=False)
    e = paths.cache_root() / 'input' / 'k1'; e.mkdir(parents=True); (e / 'f').write_bytes(b'y' * 5000)
    monkeypatch.setattr(clean, 'other_testkit_processes', lambda: ['1 python -m testkit run'])
    assert clean.clean(purge=True) == 1 and e.exists() and done.exists()      # another natemu-test runs: purge refuses
    clean.clean()                                         # and clean keeps fresh temp folders
    assert not done.exists() and (tmp_path / 'tmp' / 'testkit-seed-abc').exists()
    done = _run(tmp_path / 'wt', 'a')
    monkeypatch.setattr(clean, 'other_testkit_processes', lambda: [])
    clean.clean(dry=True)
    assert done.exists() and (tmp_path / 'tmp' / 'testkit-seed-abc').exists()
    clean.clean()
    assert not done.exists() and not crashed.exists() and kept.exists() and live.exists()
    assert not (tmp_path / 'tmp' / 'testkit-seed-abc').exists() and (tmp_path / 'tmp' / 'other').exists()
    assert e.exists()                                   # under the cap: clean leaves the cache
    os.utime(e, (time.time() - 60, time.time() - 60))
    clean.clean(purge=True)
    assert not e.exists() and cache.entries() == []


def test_phases_split_a_run():
    u = lambda f, a, g, ft, e: {'label': f + '/s', 'family': f, 'suite': 's', 'items': 2, 'cores': 1, 'core_class': 'P',
                                'asked': a, 'granted': g, 'first_test': ft, 'ended': e}
    d = {'run': 'r', 'started': 0, 'collected': 5,
         'prepare': {'start': 5, 'end': 20, 'waited': 0, 'reqs': [{'kind': 'artifact', 'name': 'natemu', 's': 14, 'built': True, 'ok': True}]},
         'units': [u('a', 20, 22, 23, 50), u('a', 20, 60, 61, 70), u('b', 20, 22, 24, 40)],
         'pytest_end': 72, 'cleanup': [72, 74], 'finished': 74}
    ph = {n: s for n, s, _ in timings.phases(d)}
    assert ph == {'start + collection': 5, 'preparation': 15, 'waiting for the first claim': 2, 'tests': 48,
                  'report': 2, 'cleanup': 2}
    assert timings._idle(d['units'], 22, 70) == 10      # 50..60: nothing running
    rows = {r[0]: r for r in timings.by_family(d)}
    assert rows['a'][3] == 48 and rows['a'][4] == 38 and rows['a'][5] == 2      # wall, unit time, start-up
    assert timings.brief(d)[0] == 'total 1 min 14 s'
    assert any('slowest' in l for l in timings.detail(d))
    del d['prepare']['end']; timings.brief(d)            # interrupted during preparation: still summarized


def test_clean_all_worktrees_reaches_other_checkouts(tmp_path, monkeypatch):
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'cache'))
    monkeypatch.setattr(paths, 'repo_root', lambda: tmp_path / 'wt')
    (tmp_path / 'wt' / 'testkit').mkdir(parents=True); (tmp_path / 'wt' / 'testkit' / 'requirements.lock').write_text('x')
    monkeypatch.setattr(clean.tempfile, 'gettempdir', lambda: str(tmp_path / 'tmp')); (tmp_path / 'tmp').mkdir()
    monkeypatch.setattr(clean, 'other_testkit_processes', lambda: [])
    other = tmp_path / 'other'; (other / 'testkit').mkdir(parents=True)
    cache.register_worktree(other)
    old = _run(other, 'x')
    clean.clean()
    assert old.exists()                                  # another checkout's runs are kept by default
    clean.clean(all_worktrees=True)
    assert not old.exists()
