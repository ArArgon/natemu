from _pytest.reports import TestReport

from testkit import known_bugs

BUGS = '''[bug.b1]
note = "wrong thing"
cases = ["s/c1::sub-a", "s/c2[*]"]
'''


def rep(nodeid, outcome, longrepr='boom'):
    return TestReport(nodeid, ('tests/f/family.toml', 0, nodeid), {}, outcome, None if outcome == 'passed' else longrepr, 'call')


def tracker(tmp_path):
    (tmp_path / 'tests' / 'f').mkdir(parents=True)
    (tmp_path / 'tests' / 'f' / 'known-bugs.toml').write_text(BUGS)
    known_bugs._bugs.cache_clear()
    return known_bugs.Tracker(tmp_path)


def cat(r):
    return dict(r.user_properties).get('testkit')


def test_known_failure_is_reported_not_failed(tmp_path):
    t = tracker(tmp_path)
    r = t.adjust(rep('tests/f/family.toml::s/c2[fp=fast]', 'failed'))
    assert r.skipped and cat(r) == 'known-bug' and 'b1: wrong thing' in r.longrepr[2]
    assert dict(r.user_properties)['testkit-failure'] == 'boom'


def test_known_bug_that_passes_is_flagged(tmp_path):
    r = tracker(tmp_path).adjust(rep('tests/f/family.toml::s/c2[fp=fast]', 'passed'))
    assert r.passed and cat(r) == 'known-bug-passed'


def test_case_failing_only_through_known_sub_results_is_known(tmp_path):
    t = tracker(tmp_path)
    t.adjust(rep('tests/f/family.toml::s/c1::sub-a', 'failed'))
    t.adjust(rep('tests/f/family.toml::s/c1::sub-b', 'passed'))
    r = t.adjust(rep('tests/f/family.toml::s/c1', 'failed', 'exit 1'))
    assert r.skipped and cat(r) == 'known-bug'


def test_one_real_sub_result_failure_keeps_the_case_failed(tmp_path):
    t = tracker(tmp_path)
    t.adjust(rep('tests/f/family.toml::s/c1::sub-a', 'failed'))
    other = t.adjust(rep('tests/f/family.toml::s/c1::sub-z', 'failed'))
    assert other.failed
    r = t.adjust(rep('tests/f/family.toml::s/c1', 'failed', 'exit 1'))
    assert r.failed and cat(r) is None


def test_unrelated_failure_untouched(tmp_path):
    r = tracker(tmp_path).adjust(rep('tests/f/family.toml::s/c9', 'failed'))
    assert r.failed and cat(r) is None
