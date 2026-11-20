import dataclasses
import pathlib

from testkit import select
from testkit.family import Family


def fam(name, covers):
    return Family(name, pathlib.Path('.'), covers, None, [], {}, {})


FAMS = [fam('fp', ['crates/runtime/src/**/fp*', 'tests/lib/fp_deviations.py']), fam('io', ['crates/host/src/virtio/**'])]


def test_affected_by_path():
    sel, unmapped, shared = select.affected(FAMS, ['crates/runtime/src/x/fpu_state.rs'], shared=[])
    assert sel == {'fp'} and not unmapped and not shared


def test_unmapped_listed():
    sel, unmapped, _ = select.affected(FAMS, ['docs/x.md', 'crates/host/src/virtio/blk.rs'], shared=[])
    assert sel == {'io'} and unmapped == ['docs/x.md']


def test_shared_selects_smoke():
    sel, unmapped, shared = select.affected(FAMS, ['testkit/runloop.py'], shared=['testkit/**'])
    assert shared and not unmapped and sel == set()


def test_folder_rule_covers_contents():
    assert select.matches('third_party/FEX/a/b.cpp', ['third_party/FEX'])
    assert not select.matches('third_party/FEXX', ['third_party/FEX'])


def test_empty_selection_exit_5_and_gate_fails(pytester, family_dir, monkeypatch, tmp_path):
    monkeypatch.setenv('NATEMU_TEST_HOST_FEATURES', 'afp=1')
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    family_dir('fam')
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-inprocess', '--tier', 'campaign', 'tests')
    assert r.ret == 5 and 'nothing selected for tier campaign' in r.stdout.str()
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-inprocess', '--gate', '--tier', 'campaign', 'tests')
    assert r.ret == 1 and 'nothing selected' in r.stdout.str()


def test_smoke_tier(pytester, family_dir, monkeypatch, tmp_path):
    monkeypatch.setenv('NATEMU_TEST_HOST_FEATURES', 'afp=1')
    family_dir('fam')
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-inprocess', '--tier', 'smoke', 'tests')
    assert '4 passed' in r.stdout.str() and '8 deselected' in r.stdout.str(), r.stdout.str()


def test_gate_combines_coverage_across_runs(pytester, family_dir, monkeypatch, tmp_path):
    family_dir('fam')
    monkeypatch.setenv('NATEMU_TEST_HOST_FEATURES', 'afp=1')               # "the M4": runs every variant
    monkeypatch.setenv('TESTKIT_RUN_DIR', str(tmp_path / 'm4'))
    (tmp_path / 'm4').mkdir()
    assert pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-inprocess', '--gate', 'tests').ret == 0
    monkeypatch.setenv('NATEMU_TEST_HOST_FEATURES', 'afp=0')               # "the M2": the afp=True variants are not runnable
    monkeypatch.setenv('TESTKIT_RUN_DIR', str(tmp_path / 'm2'))
    (tmp_path / 'm2').mkdir()
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-inprocess', '--gate', 'tests')
    assert r.ret == 1 and 'DID NOT RUN ON THIS MACHINE' in r.stdout.str(), r.stdout.str()     # not covered: needs a person's acceptance
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-inprocess', '--gate', '--rootdir', str(pytester.path),
                                      '--with-run', str(tmp_path / 'm4'), 'tests')
    assert r.ret == 0 and 'DID NOT RUN ON THIS MACHINE' not in r.stdout.str(), r.stdout.str() # covered by the M4's run: nothing to accept


def test_a_capable_machine_completes_a_gate_by_running_only_what_was_missing(pytester, family_dir, monkeypatch, tmp_path):
    family_dir('fam')
    monkeypatch.setenv('NATEMU_TEST_HOST_FEATURES', 'afp=0')               # "the M2": the afp=True variants are not runnable
    monkeypatch.setenv('TESTKIT_RUN_DIR', str(tmp_path / 'm2'))
    (tmp_path / 'm2').mkdir()
    assert pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-inprocess', '--gate', 'tests').ret == 1
    monkeypatch.setenv('NATEMU_TEST_HOST_FEATURES', 'afp=1')               # "the M4" runs just those
    monkeypatch.setenv('TESTKIT_RUN_DIR', str(tmp_path / 'm4'))
    (tmp_path / 'm4').mkdir()
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-inprocess', '--gate', '--rootdir', str(pytester.path),
                                      '--complete', str(tmp_path / 'm2'), 'tests')
    out = r.stdout.str()
    assert r.ret == 0 and '6 of its 6 not-runnable tests selected here' in out and '6 passed' in out and 'DID NOT RUN ON THIS MACHINE' not in out, out
    # option 1, on the first machine again: nothing runs, the acceptance closes its own gate
    monkeypatch.setenv('NATEMU_TEST_HOST_FEATURES', 'afp=0')
    monkeypatch.setenv('TESTKIT_RUN_DIR', str(tmp_path / 'm2b'))
    (tmp_path / 'm2b').mkdir()
    args = ('-p', 'testkit.plugin', '--testkit-inprocess', '--gate', '--rootdir', str(pytester.path), '--complete', str(tmp_path / 'm2'))
    assert pytester.runpytest_subprocess(*args, 'tests').ret == 1
    r = pytester.runpytest_subprocess(*args, '--accept-not-runnable', 'tests')
    assert r.ret == 0 and 'passed WITHOUT them' in r.stdout.str() and ' passed' not in r.stdout.str().split('DID NOT RUN')[0].split('short test summary')[-1], r.stdout.str()


def test_double_star_matches_zero_folders():
    assert select.matches('crates/runtime/src/fp.rs', ['crates/runtime/src/**/fp*'])
    assert select.matches('crates/runtime/src/a/b/fp.rs', ['crates/runtime/src/**/fp*'])
