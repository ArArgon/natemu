import json
import sys

from testkit import cli, family, lock, paths

sys.path.insert(0, str(paths.repo_root() / 'tools' / 'reference'))
import answers  # noqa: E402

FAMILY = '''[family]
name = "f"
[[suite]]
name = "f"
kind = "judged"
harness = "runner"
[suite.cases.c1]
argv = ["tools/run_x.py"]
'''


def entry(key, sha, value=None, provisional=False):
    return json.dumps({'key': key, 'sha256': sha, 'value': value, 'aux': {'mask': 2261},
                       'repro': {'supplier': 'qemu-user', 'pin': 'p', 'args': ['-cpu', 'max']},
                       **({'provisional': True} if provisional else {})})


def test_seed_logs_round_trip_into_the_family_lock(tmp_path):
    d = tmp_path / 'tests' / 'f'; d.mkdir(parents=True); (d / 'family.toml').write_text(FAMILY)
    fam = family.load(d / 'family.toml')
    runs = []
    for n, prov in (('r1', 'aa'), ('r2', 'bb')):          # the provisional answer differs between the two seeds
        a = tmp_path / n / 'answers'; a.mkdir(parents=True)
        (a / 'c1.jsonl').write_text('\n'.join([entry('k/one', '11', '{"x":1}'), entry('k/prov', prov, provisional=True)]) + '\n')
        (a / 'other.jsonl').write_text(entry('k/elsewhere', '22') + '\n')       # a case of another family: ignored
        runs.append(str(tmp_path / n))
    assert cli.lock_answers(fam, runs) == 0
    got = answers.read_lock(str(lock.path_of(fam)))
    assert set(got) == {'k/one'}
    assert got['k/one'].sha256 == '11' and got['k/one'].value == '{"x":1}' and got['k/one'].aux == {'mask': 2261}
    assert got['k/one'].repro['args'] == ['-cpu', 'max']
    (tmp_path / 'r3' / 'answers').mkdir(parents=True)
    (tmp_path / 'r3' / 'answers' / 'c1.jsonl').write_text(entry('k/one', '99') + '\n')
    assert cli.lock_answers(fam, [str(tmp_path / 'r3')]) == 1                    # a changed answer is refused
    assert answers.read_lock(str(lock.path_of(fam)))['k/one'].sha256 == '11'
