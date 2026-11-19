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


def test_the_import_closure_follows_repo_local_imports(tmp_path):
    (tmp_path / 'tools').mkdir()
    (tmp_path / 'gen.py').write_text('import os, helper\nfrom pkg import sub\ndef f():\n    import late\n')
    (tmp_path / 'helper.py').write_text('import deep\n')
    (tmp_path / 'deep.py').write_text('import helper\n')        # a cycle
    (tmp_path / 'late.py').write_text('')
    (tmp_path / 'pkg').mkdir(); (tmp_path / 'pkg' / '__init__.py').write_text('')
    (tmp_path / 'pkg' / 'sub.py').write_text('from . import sib\n')
    (tmp_path / 'pkg' / 'sib.py').write_text('')
    (tmp_path / 'tools' / 'raw_root.py').write_text('')      # tools/ is on the generators' path too
    (tmp_path / 'gen2.py').write_text('from raw_root import RAW_ROOT\n')
    got = {p.relative_to(tmp_path).as_posix() for p in lock.import_closure(tmp_path / 'gen.py', tmp_path)}
    assert got == {'gen.py', 'helper.py', 'deep.py', 'late.py', 'pkg/__init__.py', 'pkg/sub.py', 'pkg/sib.py'}
    got = {p.relative_to(tmp_path).as_posix() for p in lock.import_closure(tmp_path / 'gen2.py', tmp_path)}
    assert got == {'gen2.py', 'tools/raw_root.py'}


def test_the_real_generators_closures_name_their_imports():
    """Review: the corpus cache key missed ref.py (it assembles every program), gen_i1.py (i7) and gen_i9.py (sha, xsave)."""
    ref = paths.repo_root() / 'tools' / 'reference'
    for gen, want in (('gen_xsave.py', {'gen_i9.py', 'gen_slice1.py', 'ref.py', 'flags.py'}),
                      ('gen_i7.py', {'gen_i1.py', 'ref.py'}), ('gen_sha.py', {'gen_i9.py', 'ref.py'})):
        names = {p.name for p in lock.import_closure(ref / gen, paths.repo_root())}
        assert want <= names, (gen, names)


def test_an_input_cache_key_changes_with_a_generator_import_but_its_fingerprint_does_not(fam, tmp_path):
    """The lock's input fingerprint stays as locked (changing it would make every locked suite stale); the cache key of
    the built corpus covers the generator's imports, so an edited import rebuilds it (the corpus hash locked with the
    answers then catches a changed corpus)."""
    (tmp_path / 'gen.py').write_text('import helper\n')
    (tmp_path / 'helper.py').write_text('A = 1\n')
    f = fam()
    fp, key = lock.fingerprint_input(f, 'c'), lock.input_cache_key(f, 'c')
    (tmp_path / 'helper.py').write_text('A = 2\n')
    assert lock.fingerprint_input(f, 'c') == fp and lock.input_cache_key(f, 'c') != key
