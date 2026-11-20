import dataclasses

import pytest

from testkit import family
from testkit.host import Host
from testkit.tests.conftest import FIXTURES

H_AFP = Host('0' * 36, 'Apple M4', 4, 6, 'x', {'afp': True})
H_NOAFP = Host('0' * 36, 'Apple M2 Max', 8, 4, 'x', {'afp': False})


def suite():
    return family.load(FIXTURES / 'fam' / 'family.toml').suites[0]


def test_expand_ids():
    assert [v.id for v in family.expand(suite())] == \
        ['afp=False,fp=accurate', 'afp=False,fp=fast', 'afp=True,fp=accurate', 'afp=True,fp=fast']


def test_exclusive_needs_why(tmp_path):
    (tmp_path / 'family.toml').write_text('[[suite]]\nname = "x"\nkind = "judged"\nharness = "h"\nneeds = { exclusive = true }\n')
    with pytest.raises(family.FamilyError, match='why'):
        family.load(tmp_path / 'family.toml')


def test_placement_hidden_and_real():
    v_off = next(v for v in family.expand(suite()) if v.get('host.afp') is False)
    v_on = next(v for v in family.expand(suite()) if v.get('host.afp') is True)
    p = family.placement(v_off, H_AFP)
    assert p.runnable and dict(p.met) == {'afp': 'hidden'} and dict(p.env) == {'NATEMU_FEXC_FEATURE_OFF': 'afp'}
    p = family.placement(v_off, H_AFP, frozenset({'afp'}))
    assert not p.runnable and p.reason == 'needs afp absent (real)'
    p = family.placement(v_on, H_NOAFP)
    assert not p.runnable and p.reason == 'needs afp'
    assert family.placement(v_off, H_NOAFP).met == (('afp', 'real'),)


def test_collect_reports_excluded_with_reason(pytester, family_dir, monkeypatch):
    monkeypatch.setenv('NATEMU_TEST_HOST_FEATURES', 'afp=0')
    family_dir('fam')
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-inprocess', '-rs', 'tests')
    out = r.stdout.str()
    assert '5 passed' in out and '1 excluded' in out and '6 not-runnable' in out, out
    assert 'excluded: nan-results' in out and 'not-runnable: needs afp' in out


def test_gate_needs_a_person_to_pass_without_not_runnable(pytester, family_dir, monkeypatch):
    monkeypatch.setenv('NATEMU_TEST_HOST_FEATURES', 'afp=0')
    family_dir('fam')
    assert pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-inprocess', 'tests').ret == 0
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-inprocess', '--gate', 'tests')
    assert r.ret == 1 and 'BUT 6 TESTS DID NOT RUN ON THIS MACHINE' in r.stdout.str() and 'gate: not passed: incomplete' in r.stdout.str()
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-inprocess', '--gate', '--accept-not-runnable', 'tests')
    assert r.ret == 0 and 'BUT 6 TESTS DID NOT RUN ON THIS MACHINE' in r.stdout.str() and 'passed WITHOUT them' in r.stdout.str()


def test_m1_efficiency_cores_have_no_tso():
    from testkit import host
    assert host.efficiency_cores_lack_tso('Apple M1') and host.efficiency_cores_lack_tso('Apple M1 Max')
    assert not host.efficiency_cores_lack_tso('Apple M2') and not host.efficiency_cores_lack_tso('Apple M4 Pro')
