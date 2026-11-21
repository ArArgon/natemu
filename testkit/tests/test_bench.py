import os
import pathlib
import stat
import subprocess

import pytest

from testkit import artifacts
from testkit.prepare import BuildFailure


def test_builder_setting(monkeypatch, tmp_path):
    monkeypatch.delenv('NATEMU_BENCH_BUILDER', raising=False)
    monkeypatch.setattr(artifacts, 'local_config', lambda: {})
    assert artifacts.bench_builder() == 'local'
    monkeypatch.setattr(artifacts, 'local_config', lambda: {'bench': {'builder': 'ssh:me@box'}})
    assert artifacts.bench_builder() == 'ssh:me@box'
    monkeypatch.setenv('NATEMU_BENCH_BUILDER', 'local')
    assert artifacts.bench_builder() == 'local'
    monkeypatch.setenv('NATEMU_BENCH_BUILDER', 'docker')
    with pytest.raises(BuildFailure):
        artifacts.bench_builder()


def _bench_family():
    from testkit import family, paths
    fam = family.load(paths.repo_root() / 'tests' / 'bench' / 'family.toml')
    return fam, {s.name: s for s in fam.suites}


def test_m1_perf_requires_its_builds_and_inputs():
    """The m1-perf suite prepares the baseline build, the matrix and IO5 inputs and qemu-img; the other benchmarks do
    not (preparing a bench run of one guest benchmark must not build a second natemu)."""
    from testkit.harness import Requirement
    from testkit.harness.bench import HARNESS
    fam, suites = _bench_family()
    m1 = suites['m1-perf']
    assert m1.extra['tier'] == 'bench' and m1.params['timeout'] == 7200 and list(m1.extra['cases']) == ['m1-perf']
    reqs = set(HARNESS.requirements(fam, m1, None))
    assert {Requirement('natemu-at', 'ecec5d77'), Requirement('input', 'fio-prep'), Requirement('input', 'block-inputs'),
            Requirement('input', 'io5-inputs'), Requirement('artifact', 'qemu-system'), Requirement('artifact', 'natemu'),
            Requirement('artifact', 'fexc')} <= reqs
    assert Requirement('artifact', 'bench-image') not in reqs           # no guest benchmark in it
    other = HARNESS.requirements(fam, suites['bench'], None)
    assert Requirement('artifact', 'bench-image') in other
    assert not {r for r in other if r.kind in ('natemu-at', 'input')}
    paper = set(HARNESS.requirements(fam, suites['paper'], None))           # guest-paper: its own suite, for the same reason
    assert {Requirement('input', 'io5-inputs'), Requirement('input', 'paper-inputs')} <= paper
    assert not {r for r in paper if r.kind == 'natemu-at'} and Requirement('artifact', 'bench-image') not in paper


def test_m1_perf_baseline_is_the_runners():
    """The baseline the case builds is the commit run_m1_perf.py records as its baseline."""
    import re
    from testkit import paths
    _, suites = _bench_family()
    src = (paths.repo_root() / 'tests' / 'bench' / 'run_m1_perf.py').read_text()
    assert suites['m1-perf'].extra['cases']['m1-perf']['baseline'] == re.search(r"^BASELINE_COMMIT = '(\w+)'", src, re.M)[1]


def _m1_perf_harness(monkeypatch, tmp_path):
    """The bench harness with every build, input and the script itself stubbed; returns run(run_dir, final=...) ->
    (the case's argv, its env, the case folder, the result)."""
    from types import SimpleNamespace
    from testkit import host, inputs, resources, runloop
    from testkit.harness import bench
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'cache'))
    fam, suites = _bench_family()
    entry = lambda name: artifacts.Natemu(tmp_path / name, name)
    ro = {}
    for n in ('fio-prep', 'block-inputs', 'io5-inputs'):
        ro[n] = tmp_path / 'entries' / n; ro[n].mkdir(parents=True); (ro[n] / 'f').write_text(n); (ro[n] / 'f').chmod(0o444)
    final = {'name': 'final'}
    monkeypatch.setattr(artifacts, 'natemu', lambda: entry(final['name']))
    monkeypatch.setattr(artifacts, 'natemu_at', lambda rev: entry('base-' + rev))
    monkeypatch.setattr(artifacts, 'fexc', lambda: tmp_path / 'fexc')
    monkeypatch.setattr(artifacts, 'qemu_system', lambda: tmp_path / 'qemu' / 'bin')
    monkeypatch.setattr(inputs, 'fio_prep', lambda: ro['fio-prep'])
    monkeypatch.setattr(inputs, 'block_inputs', lambda: ro['block-inputs'])
    monkeypatch.setattr(inputs, 'io5_inputs', lambda: ro['io5-inputs'])
    monkeypatch.setattr(resources, 'fetch', lambda n: tmp_path / n)
    monkeypatch.setattr(host, 'detect', lambda: SimpleNamespace(machine_id='M', chip='Apple M1'))
    rd = {}
    monkeypatch.setattr(runloop, 'run_dir', lambda config: rd['dir'])
    monkeypatch.setattr('testkit.sched.pool.raw_root', lambda: None)
    seen, real, codes = {}, subprocess.run, {'rc': 0}
    def fake(argv, **kw):
        if 'tests/bench/run_m1_perf.py' not in argv: return real(argv, **kw)       # the clones' cp
        seen.update(argv=argv, env=kw['env']); return SimpleNamespace(returncode=codes['rc'])
    monkeypatch.setattr(subprocess, 'run', fake)
    item = SimpleNamespace(suite=suites['m1-perf'], case='m1-perf', nodeid='m1')

    def run(run_dir, final_name='final', fresh=False, case='m1-perf', rc=0):
        rd['dir'], final['name'] = tmp_path / run_dir, final_name
        item.case = case; codes['rc'] = rc
        res = bench.HARNESS.run([item], SimpleNamespace(getoption=lambda o: fresh if o == 'fresh' else None))
        return seen['argv'], seen['env'], tmp_path / run_dir / 'bench' / 'M' / 'm1-perf', res['m1']
    return run


def test_m1_perf_argv_gets_framework_builds_and_inputs(monkeypatch, tmp_path):
    """Every input placeholder is filled from artifacts/inputs: nothing points into natemu-raw. The matrix preparation's
    sources and the IO5 inputs are writable clones in the case folder (run_io_matrix.py prepare writes into what it
    copies; run_io5.py grows its clone of disk.raw, which keeps the read-only mode of the cache entry)."""
    a, env, out, res = _m1_perf_harness(monkeypatch, tmp_path)('run1')
    assert res.outcome == 'passed', res
    val = lambda opt: a[a.index(opt) + 1]
    assert a[1:3] == ['tests/bench/run_m1_perf.py', 'full'] and '--retry-failed' in a
    assert val('--baseline-build') == str(tmp_path / 'base-ecec5d77') and val('--final-build') == str(tmp_path / 'final')
    assert val('--fexc') == str(tmp_path / 'fexc')
    assert val('--inputs') == val('--dir') + '/matrix-inputs'
    for opt, n in (('--fio-prep', 'fio-prep'), ('--block-inputs', 'block-inputs'), ('--io5-inputs', 'io5-inputs')):
        p = pathlib.Path(val(opt))
        assert p.is_relative_to(out) and (p / 'f').read_text() == n and os.access(p / 'f', os.W_OK)
    assert env['NATEMU_QEMU_IMG'] == str(tmp_path / 'qemu' / 'bin' / 'qemu-img')
    assert env['NATEMU_VM_LOCK_HELD'] == '1'
    assert 'natemu-raw' not in ' '.join(a)


def test_m1_perf_rerun_resumes_in_the_same_folder(monkeypatch, tmp_path):
    """The results folder (--dir) outlives the run: a second run of the case with the same builds and inputs gets the
    same folder (so run_m1_perf.py resumes there), and a new final build gets a new one."""
    from testkit import paths
    run = _m1_perf_harness(monkeypatch, tmp_path)
    val = lambda a, opt: a[a.index(opt) + 1]
    a1, _, out1, r1 = run('run1')
    a2, _, out2, _ = run('run2')
    d = pathlib.Path(val(a1, '--dir'))
    assert out1 != out2 and val(a2, '--dir') == str(d)
    assert d.is_dir() and d.is_relative_to(paths.state_dir()) and not d.is_relative_to(out1)
    assert str(d) in r1.message                      # the result says where the summary is
    a3, *_ = run('run3', final_name='final-changed')
    assert val(a3, '--dir') != str(d)


def test_ssh_builder_runs_recipe_and_cleans_up(tmp_path):
    """A stand-in ssh/scp that runs commands locally: the recipe is copied, build.sh runs, outputs come back, and the
    remote temporary folder is removed."""
    remote = tmp_path / 'remote'; remote.mkdir()
    log = tmp_path / 'ssh.log'
    fake_ssh = tmp_path / 'ssh'
    fake_ssh.write_text(f'#!/bin/sh\n# skip -o OPT pairs and the host\nwhile [ "$1" = -o ]; do shift 2; done; shift\n'
                        f'echo "$@" >> {log}\nexport TMPDIR={remote}\nexport HOME={remote}\nexec sh -c "$*"\n')
    fake_scp = tmp_path / 'scp'
    fake_scp.write_text('#!/bin/sh\nwhile [ "$1" = -q ] || [ "$1" = -o ]; do [ "$1" = -o ] && shift; shift; done\n'
                        'cp "${1#*:}" "$2"\n')
    for f in (fake_ssh, fake_scp): f.chmod(f.stat().st_mode | stat.S_IEXEC)
    recipe = tmp_path / 'recipe' / 'scripts'; recipe.mkdir(parents=True)
    (recipe / 'build.sh').write_text('mkdir -p "$1/out"; echo cpio > "$1/out/bench.cpio"; echo exp > "$1/out/expected.txt"\n')
    out = tmp_path / 'out'; out.mkdir()
    artifacts._build_bench_ssh(recipe.parent, out, 'me@box', ssh=str(fake_ssh), scp=str(fake_scp))
    assert (out / 'bench.cpio').read_text() == 'cpio\n' and (out / 'expected.txt').read_text() == 'exp\n'
    assert list(remote.iterdir()) == []                         # nothing left on the builder
    assert 'rm -rf' in log.read_text()



def test_m1_perf_resume_folder_changes_with_its_scripts(monkeypatch, tmp_path):
    """Review of Task 4: a changed run_io_matrix.py (job list, profiles) must not resume old records or meet inputs the
    old script prepared."""
    from testkit.harness import bench
    run = _m1_perf_harness(monkeypatch, tmp_path)
    val = lambda a, opt: a[a.index(opt) + 1]
    a1, *_ = run('run1')
    script = tmp_path / 'tests' / 'io' / 'run_io_matrix.py'; script.parent.mkdir(parents=True); script.write_text('v1')
    monkeypatch.setattr(bench, 'RESUME_SCRIPTS', ('tests/io/run_io_matrix.py',))
    monkeypatch.setattr(bench.paths, 'repo_root', lambda: tmp_path)
    a2, *_ = run('run2')
    script.write_text('v2')
    a3, *_ = run('run3')
    assert len({val(a, '--dir') for a in (a1, a2, a3)}) == 3


def test_m1_perf_fresh_sets_the_old_records_aside(monkeypatch, tmp_path):
    """--fresh measures everything again (the records are kept beside, for `natemu-test clean`)."""
    run = _m1_perf_harness(monkeypatch, tmp_path)
    val = lambda a, opt: a[a.index(opt) + 1]
    a1, *_ = run('run1')
    d = pathlib.Path(val(a1, '--dir')); (d / 'runs.jsonl').write_text('{"id": "A-base-raw-0", "ok": true}\n')
    a2, *_ = run('run2', fresh=True)
    assert val(a2, '--dir') == str(d) and not (d / 'runs.jsonl').exists()
    assert [p for p in d.parent.iterdir() if '.set-aside-' in p.name]


def test_clean_removes_old_resume_folders(monkeypatch, tmp_path):
    import time
    from testkit import clean, paths
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'cache'))
    base = paths.state_dir() / 'bench-resume' / 'm1-perf'
    old, new = base / 'a', base / 'b'
    for f in (old, new): f.mkdir(parents=True); (f / 'runs.jsonl').write_text('x')
    os.utime(old, (time.time() - 20 * 86400,) * 2)
    assert clean.resume_folders(14 * 86400) == [old]
    assert sorted(clean.resume_folders(None)) == [old, new]



def test_m1_perf_runs_as_pieces_that_share_one_resume_folder(monkeypatch, tmp_path):
    """Replay showed an indivisible 80-minute exclusive unit blocks every other run; as one piece per planned run
    (--limit 1) the scheduler can interleave other work between them. All pieces resume in one folder."""
    from testkit.harness import bench
    fam, suites = _bench_family()
    names = bench.HARNESS.cases(fam, suites['m1-perf'])
    assert len(names) == 21 and names[0] == 'm1-perf/piece-00'
    run = _m1_perf_harness(monkeypatch, tmp_path)
    val = lambda a, opt: a[a.index(opt) + 1]
    a1, _, _, r1 = run('run1', case='m1-perf/piece-00', rc=4)
    a2, _, _, r2 = run('run1', case='m1-perf/piece-07', rc=0)
    whole, *_ = run('run2', case='m1-perf')
    assert a1[-2:] == ['--limit', '1'] and '--limit' not in whole
    assert '--retry-failed' not in a1 and '--retry-failed-before' in a1 and '--retry-failed' in whole
    assert val(a1, '--dir') == val(a2, '--dir') == val(whole, '--dir')
    assert r1.outcome == 'passed' and 'more left' in r1.message and r2.outcome == 'passed'
    _, _, _, r3 = run('run1', case='m1-perf/piece-03', rc=1)
    assert r3.outcome == 'failed'



def test_m1_perf_pieces_fresh_once_whatever_piece_runs_first(monkeypatch, tmp_path):
    """Review: pieces run in any order (longest first); --fresh must set the old records aside once per run, not when
    piece-00 happens to run (possibly last, discarding the run's own measurements)."""
    run = _m1_perf_harness(monkeypatch, tmp_path)
    val = lambda a, opt: a[a.index(opt) + 1]
    a0, *_ = run('run0', case='m1-perf/piece-00', rc=4)
    d = pathlib.Path(val(a0, '--dir')); (d / 'runs.jsonl').write_text('old\n')
    run('run1', case='m1-perf/piece-09', fresh=True, rc=4)            # first in this run: sets the old records aside
    assert not (d / 'runs.jsonl').exists()
    (d / 'runs.jsonl').write_text('new\n')
    run('run1', case='m1-perf/piece-00', fresh=True, rc=4)            # later in the same run: keeps the new ones
    assert (d / 'runs.jsonl').read_text() == 'new\n'


def test_m1_perf_runs_left_after_every_piece_fail_loudly(monkeypatch, tmp_path):
    """Review: a retry or a crash shifts pieces against runs; the last piece then still sees runs left. That must fail,
    not pass with no verdict."""
    run = _m1_perf_harness(monkeypatch, tmp_path)
    for i in range(20): run('run1', case=f'm1-perf/piece-{i:02d}', rc=4)
    *_, last = run('run1', case='m1-perf/piece-20', rc=4)
    assert last.outcome == 'failed' and 'runs left after all 21 pieces' in last.message


def test_a_failing_piece_count_is_loud(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from testkit.harness import bench
    monkeypatch.setattr(bench, '_PIECES', {})
    monkeypatch.setattr(bench.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr='boom'))
    with pytest.raises(RuntimeError, match='no piece count'):
        bench._piece_count({'argv': ['python3', 'tests/bench/run_m1_perf.py', 'full']})
