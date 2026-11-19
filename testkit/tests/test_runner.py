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


def test_migration_maps_every_step():
    import pathlib, sys
    from testkit import migrate, paths, family
    sys.path.insert(0, str(paths.repo_root() / 'tools'))
    import run_suite
    steps = {s.name for s in run_suite.steps('F', pathlib.Path('/O'))}
    covered = {s for v in migrate.COVERED.values() for s in v}
    in_families = set()
    for fam in ('static', 'boot', 'io', 'faults', 'smp', 'system'):
        f = family.load(paths.repo_root() / 'tests' / fam / 'family.toml')
        in_families |= set(f.suite(fam).extra['cases'])
    assert steps == covered | in_families and not (covered & in_families)


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


def test_hand_written_io_suites_survive_regeneration():
    """testkit.migrate rewrites tests/io/family.toml with the generated `io` suite only; these suites are added by hand."""
    from testkit import paths, family
    f = family.load(paths.repo_root() / 'tests' / 'io' / 'family.toml')
    want = {'io-selfcheck': {'io-stress-selfcheck', 'lifecycle-stress-selfcheck', 'virtio-net-selfcheck', 'io-pending-compiler-selfcheck'},
            'io-program': {'virtio-net', 'io-windows', 'io-window-cases', 'io-pending-compiler'},
            'io-campaign': {'io-stress-n2', 'io-stress-n4', 'lifecycle-reset', 'lifecycle-disable', 'lifecycle-corrupt-control',
                            'serial-unpaced', 'serial-paced', 'serial-loaded', 'soak'}}
    for suite, cases in want.items():
        assert cases <= set(f.suite(suite).extra['cases']), suite

