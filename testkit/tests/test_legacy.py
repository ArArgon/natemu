from testkit import family, paths
from testkit.harness import legacy


def fam():
    return family.load(paths.repo_root() / 'tests' / 'legacy' / 'family.toml')


def test_every_step_mapped():
    f = fam(); s = f.suite('steps')
    cases = set(legacy.HARNESS.cases(f, s))
    for name in legacy.steps():
        assert name in cases or name in legacy.BUILDS or name in s.params['replaced'], name


def test_serial_steps_exclusive_with_why():
    f = fam(); s = f.suite('steps')
    rs = legacy._suite_module()
    for name in rs.SERIAL_WHY:
        if name in legacy.steps():
            n = legacy.HARNESS.case_needs(f, s, name)
            assert n.exclusive and n.why == rs.SERIAL_WHY[name]
    assert legacy.HARNESS.case_needs(f, s, 'l1').exclusive
    assert not legacy.HARNESS.case_needs(f, s, 'fix').perf             # E_OK: may use efficiency cores
    assert legacy.HARNESS.case_needs(f, s, 'smp').perf
