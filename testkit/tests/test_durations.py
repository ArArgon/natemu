from testkit import durations


def test_record_and_expected(monkeypatch, tmp_path):
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path))
    assert durations.expected('a') is None
    durations.record('a', 10); durations.record('a', 20)
    assert durations.expected('a') == 13.0
    durations.record_many({'a': 13.0, 'b': 5})
    assert durations.expected('a') == 13.0 and durations.expected('b') == 5
