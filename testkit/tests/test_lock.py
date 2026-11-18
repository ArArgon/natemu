import shutil

import pytest

from testkit import family, lock, paths

FAM = '''[family]
name = "lk"
[input.c]
generator = "gen.py"
args = ["--seed", "1"]
[[suite]]
name = "s"
kind = "deterministic"
harness = "programs"
input = "c"
reference = "qemu"
needs = { cores = 1 }
smoke = ["a"]
batch = 4
'''


@pytest.fixture
def fam(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, 'repo_root', lambda: tmp_path)
    (tmp_path / 'gen.py').write_text('print(1)\n')
    d = tmp_path / 'tests' / 'lk'; d.mkdir(parents=True)
    (d / 'family.toml').write_text(FAM)
    family.load.cache_clear()

    def load(text=FAM):
        (d / 'family.toml').write_text(text); family.load.cache_clear()
        return family.load(d / 'family.toml')
    return load


def seed(f, answers, tool='qemu-x86_64-static'):
    s = f.suites[0]
    lock.write(f, {'suite': {'s': {'fingerprint': lock.fingerprint_suite(f, s), 'reference': 'qemu',
                                   'seeded': {'tool': tool, 'pin': 'p', 'machine': 'm', 'date': 'd'}, 'cases': answers}}})


def test_fingerprint_ignores_scheduling_fields(fam):
    f0 = fam()
    a = lock.fingerprint_suite(f0, f0.suites[0])
    f1 = fam(FAM.replace('cores = 1', 'cores = 4').replace('batch = 4', 'batch = 64').replace('smoke = ["a"]', 'smoke = true'))
    assert lock.fingerprint_suite(f1, f1.suites[0]) == a
    f2 = fam(FAM.replace('"1"]', '"2"]'))
    assert lock.fingerprint_suite(f2, f2.suites[0]) != a


def test_stale_detected(fam, tmp_path):
    f = fam()
    assert lock.status(f, f.suites[0]) == 'missing'
    seed(f, {'a': 'x'})
    assert lock.status(f, f.suites[0]) == 'ok'
    (tmp_path / 'gen.py').write_text('print(2)\n')
    assert lock.status(f, f.suites[0]) == 'stale'


def test_write_is_stable(fam):
    f = fam()
    seed(f, {'b': '2', 'a': '1'})
    first = lock.path_of(f).read_bytes()
    lock.write(f, lock.read(f))
    assert lock.path_of(f).read_bytes() == first


def test_check_flags_collapsed_answers(fam):
    f = fam()
    seed(f, {f'c{i}': 'same' for i in range(30)})
    assert any('share 1 answers' in p for p in lock.check(f))


def test_check_flags_wrong_reference_and_missing(fam):
    f = fam()
    seed(f, {'a': 'x', 'b': ''}, tool='natemu')
    probs = lock.check(f)
    assert any('seeded by natemu' in p for p in probs)
    assert any('1 cases have no answer yet (b)' in p for p in probs)
