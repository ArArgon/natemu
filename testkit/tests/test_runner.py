import json

from testkit.harness import runner


def write(tmp_path, data):
    (tmp_path / 'results.json').write_text(json.dumps(data))
    return runner.per_case(tmp_path, 'results.json')


def test_results_list_shape(tmp_path):
    r = write(tmp_path, {'counts': {}, 'results': [{'name': 'a', 'outcome': 'match', 'diffs': []},
                                                   {'name': 'b', 'outcome': 'fail', 'diffs': ['rax: 1 vs 2']},
                                                   {'name': 'c', 'outcome': 'unsupported', 'diffs': []}]})
    assert r == [('a', 'passed', ''), ('b', 'failed', 'rax: 1 vs 2'), ('c', 'skipped', '')]


def test_top_level_list_shape(tmp_path):
    r = write(tmp_path, [{'name': 'x', 'ok': True, 'problems': []}, {'name': 'y', 'ok': False, 'problems': ['bad']},
                         {'name': 'z', 'errors': ['boom']}])
    assert r == [('x', 'passed', ''), ('y', 'failed', 'bad'), ('z', 'failed', 'boom')]


def test_single_result_shape(tmp_path):
    assert write(tmp_path, {'pass': True, 'errors': []}) == [('result', 'passed', '')]
    assert write(tmp_path, {'pass': False, 'errors': ['x', 2]}) == [('result', 'failed', 'x; 2')]
    assert write(tmp_path, {'pass': {'a': True}}) == [('a', 'passed', '')]       # a pass map still matches first


def test_judge_runs_on_exit_zero_only_when_asked(tmp_path):
    (tmp_path / 'result.json').write_text(json.dumps({'pass': True, 'errors': []}))
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location('j', pathlib.Path(__file__).parents[2] / 'tests/io/judges.py')
    j = importlib.util.module_from_spec(spec); spec.loader.exec_module(j)
    assert j.must_fail(tmp_path)[0] is False
    (tmp_path / 'result.json').write_text(json.dumps({'pass': False, 'errors': ['e']}))
    assert j.must_fail(tmp_path) == (True, 'negative control failed as required')
    (tmp_path / 'result.json').write_text(json.dumps({'pass': False, 'errors': []}))
    assert j.must_fail(tmp_path)[0] is False


def test_missing_or_unknown_gives_none(tmp_path):
    assert runner.per_case(tmp_path, 'results.json') is None
    assert write(tmp_path, {'something': 'else'}) is None


def test_case_timeout_overrides_the_suite(tmp_path):
    from testkit import family
    d = tmp_path / 'tests' / 'f'; d.mkdir(parents=True)
    (d / 'family.toml').write_text('''[family]
name = "f"
[[suite]]
name = "s"
kind = "judged"
harness = "runner"
params = { timeout = 900 }
[suite.cases.long]
argv = ["tools/x.py"]
timeout = 2400
[suite.cases.short]
argv = ["tools/x.py"]
''')
    fam = family.load(d / 'family.toml'); s = fam.suite('s')
    assert runner.HARNESS.case_timeout(fam, s, 'long') == 2400 and runner.HARNESS.case_timeout(fam, s, 'short') is None


def test_diff_rerun_uses_live_answers_and_its_own_output_folder(tmp_path):
    """--diff: a case that failed on a hash-only answer runs once more with live QEMU answers, into its own folder."""
    import sys
    script = tmp_path / 'r.py'
    script.write_text('import os, sys\nprint("answers", os.environ["NATEMU_ANSWERS"], "NATEMU_ANSWERS_LOCK" in os.environ)\n'
                      'print("out", sys.argv[sys.argv.index("--out") + 1])\nprint("x differs: 1 != 2")\nsys.exit(1)\n')
    o = tmp_path / 'run' / 'out' / 'c'
    env = {'PATH': '/usr/bin:/bin', 'NATEMU_ANSWERS': 'locked', 'NATEMU_ANSWERS_LOCK': 'x.lock', 'NATEMU_ANSWERS_USED': 'u'}
    note = runner.HARNESS.diff_rerun([sys.executable, str(script), '--out', str(o)], env, o, tmp_path / 'run', 'c')
    assert 'answers live False' in note and f'out {o}.diff' in note and 'x differs: 1 != 2' in note
    assert 'exit 1' in note and str(tmp_path / 'run' / 'runner' / 'c.diff.log') in note
    assert env['NATEMU_ANSWERS'] == 'locked'                    # the case's own environment is untouched
    script.write_text('import sys\nprint("all fine")\n')
    note = runner.HARNESS.diff_rerun([sys.executable, str(script), '--out', str(o)], env, o, tmp_path / 'run', 'c')
    assert 'the locked answer is stale' in note and 'fails only sometimes' in note and 'all fine' not in note


def test_diff_rerun_only_for_hash_only_answers():
    from tools.reference import answers
    assert runner.needs_diff(f'step 3: {answers.HASH_ONLY}: qemu-system/abc')
    assert not runner.needs_diff('x differs: 1 != 2 (QEMU 2)')
    assert 'natemu-test run --diff' in answers.HASH_ONLY


def test_a_current_target_is_not_installed_again_so_no_exclusive_build_lock_wait(tmp_path, monkeypatch):
    """Every running runner case holds the build lock shared: with target/ current, preparation must not wait for it."""
    import fcntl, threading
    from types import SimpleNamespace as NS
    from testkit.harness import runner
    entry, target = tmp_path / 'entry', tmp_path / 'target'
    for rel in runner.LAYOUT:
        (entry / rel).parent.mkdir(parents=True, exist_ok=True); (entry / rel).write_bytes(b'signed ' + rel.encode())
    monkeypatch.setattr(runner.artifacts, 'natemu', lambda: NS(entry=entry))
    monkeypatch.setattr(runner.paths, 'repo_root', lambda: tmp_path)
    monkeypatch.setenv('CARGO_TARGET_DIR', str(target))
    runner.resolve_target(None)                                   # first time: installs, nothing else holds the lock
    assert runner.installed(NS(entry=entry))
    lock = (tmp_path / '.natemu' / 'build.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_SH)                              # a running case of another run
    try:
        t = threading.Thread(target=runner.resolve_target, args=(None,), daemon=True); t.start(); t.join(5)
        assert not t.is_alive(), 'a current target/ waited for the exclusive build lock'
        (target / runner.LAYOUT[0]).write_bytes(b'stale')        # a builder case's own build left behind
        t = threading.Thread(target=runner.resolve_target, args=(None,), daemon=True); t.start(); t.join(1)
        assert t.is_alive(), 'a stale target/ must be installed under the exclusive lock'
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN); lock.close()
    t.join(5)
    assert not t.is_alive() and runner.installed(NS(entry=entry))
