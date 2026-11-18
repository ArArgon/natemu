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
