"""The campaign harness (testkit/harness/campaign.py) and the campaigns' verdicts (tests/correctness/verdicts.py). `boot` is
replaced by a fake that returns canned serial text: nothing here starts a VM or builds an image."""
import pathlib
import re
import shutil
import textwrap

import pytest

from testkit import family, paths
from testkit.harness import campaign

REPO = paths.repo_root()
VERDICTS = REPO / 'tests' / 'correctness' / 'verdicts.py'


def verdicts():
    return family._load_module(VERDICTS, 'test_campaign_verdicts')


# --- a small campaign family in tmp_path, booted by a fake ---

def make_family(tmp_path, suite='openssl', verdict='openssl', parts=2, extra=''):
    d = tmp_path / 'tests' / 'c'
    d.mkdir(parents=True)
    shutil.copy(VERDICTS, d / 'verdicts.py')
    (d / 'family.toml').write_text(textwrap.dedent(f'''\
        [family]
        name = "c"
        [[suite]]
        name = "{suite}"
        kind = "judged"
        harness = "campaign"
        tier = "campaign"
        parts = {parts}
        cap = 60
        verdict = "verdicts:{verdict}"
        matrix = {{ "machine.vcpus" = [4] }}
        ''') + textwrap.dedent(extra))
    family.load.cache_clear()
    fam = family.load(d / 'family.toml')
    return fam, fam.suite(suite)


def make_image(tmp_path, suite, listing):
    img = tmp_path / 'image'
    img.mkdir()
    (img / f'{suite}.cpio.zst').write_bytes(b'not a real image')
    (img / f'{suite}-cases.txt').write_text(textwrap.dedent(listing))
    return img


def serial(part, *lines, end=True, rc=0, suite='openssl'):
    s = [f'[    1.0] booting', f'CT-BEGIN suite={suite} part={part} cpus=4 kernel=6.12.107', *lines]
    if end: s.append(f'CT-END suite={suite} part={part} rc={rc} guest_s=12.5')
    return '\n'.join(s) + '\n'


class FakeBoot:
    """Returns the next canned serial text for a part (the last one again once used up) and records every call."""
    def __init__(self, by_part: dict):
        self.by_part = {k: list(v) if isinstance(v, list) else [v] for k, v in by_part.items()}
        self.calls = []

    def __call__(self, image, suite, part, vcpus, work, cap, env=None):
        self.calls.append(dict(image=image, suite=suite, part=part, vcpus=vcpus, work=work, cap=cap, env=env))
        work.mkdir(parents=True, exist_ok=True)
        q = self.by_part[part]
        text = q.pop(0) if len(q) > 1 else q[0]
        (work / 'serial.txt').write_text(text)
        return campaign.BootResult('CT-END' in text, text)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def make(listing, by_part, **kw):
        fam, suite = make_family(tmp_path, **kw)
        img = make_image(tmp_path, suite.name, listing)
        monkeypatch.setattr(campaign, 'image', lambda name: img)
        fake = FakeBoot(by_part)
        monkeypatch.setattr(campaign, 'boot', fake)
        variant = family.expand(suite)[0]

        def run(case):
            return campaign.HARNESS.run_part(fam, suite, case, variant, tmp_path / 'run')
        return run, fake, img
    return make


def subs(res):
    return {s.name: (s.outcome, s.category) for s in res.sub}


# --- parsing ---

def test_parse_ct_result_lines():
    text = (
        'CT-BEGIN suite=openssl part=0/2 cpus=4 kernel=6.12.107\n'
        'CT-RESULT openssl default:test_x509 rc=0 ok=10 notok=0\n'
        '[   3.1] random kernel noise CT-RESULT openssl generic:test_rand rc=1 ok=3 notok=1\n'
        'CT-OUT generic:test_rand | not ok 4 - rand bytes\n'
        'CT-OUT generic:test_rand | Result: FAIL\n'
        'CT-LOG sigreturn_64 | [OK] it works\n'
        'CT-RESULT x86st sigreturn_64 rc=0 ok=1 fail=0 skip=0 out=0123456789ab\n'
        'CT-SKIP x86st mov_ss_trap_64\n'
        'CT-SKIP litmus SB.SB+mfences threads=8\n'
        "CT-CASE test.test_os.TimerfdTests.test_timerfd_poll (mode='a b') fail\n"
        'CT-RESULT cpy test_os cases=500 pass=490 fail=1 error=0 skip=9 sec=40.0\n'
        'CT-DMESG [    0.000] Linux version 6.12.107\n'
        'CT-END suite=openssl part=0/2 rc=0 guest_s=12.5\n')
    got = dict(campaign.parse(text))
    assert got['default:test_x509'] == {'kind': 'openssl', 'rc': '0', 'ok': '10', 'notok': '0',
                                        'extra': ['CT-DMESG [    0.000] Linux version 6.12.107']}
    r = got['generic:test_rand']
    assert (r['kind'], r['rc'], r['notok']) == ('openssl', '1', '1')
    assert r['extra'][:2] == ['CT-OUT generic:test_rand | not ok 4 - rand bytes', 'CT-OUT generic:test_rand | Result: FAIL']
    assert got['sigreturn_64']['extra'][0] == 'CT-LOG sigreturn_64 | [OK] it works'
    assert got['mov_ss_trap_64'] == {'kind': 'x86st', 'ct_skip': True, 'extra': ['CT-DMESG [    0.000] Linux version 6.12.107']}
    assert got['SB.SB+mfences']['threads'] == '8' and got['SB.SB+mfences']['ct_skip'] is True
    assert got['test_os']['fail'] == '1'
    assert "CT-CASE test.test_os.TimerfdTests.test_timerfd_poll (mode='a b') fail" in got['test_os']['extra']
    assert campaign.end_of(text) == {'suite': 'openssl', 'part': '0/2', 'rc': '0', 'guest_s': '12.5'}
    assert campaign.end_of('CT-BEGIN suite=x part=0/1\n') is None


def test_parse_stress_logs_and_public_lines():
    text = ('CT-LOG mutex | stress-ng: info:  [1] passed: 4: mutex (4)\n'
            'CT-RESULT stress mutex.r1 rc=0 n=4 pass=4 skip=0 fail=0 bogo=3867484 sec=12\n'
            'CT-DMESG [   9.0] BUG: kernel NULL pointer dereference\n')
    got = dict(campaign.parse(text))
    assert got['mutex.r1']['extra'] == ['CT-LOG mutex | stress-ng: info:  [1] passed: 4: mutex (4)',
                                        'CT-DMESG [   9.0] BUG: kernel NULL pointer dereference']
    pub = ('CT-BEGIN suite=public part=7/1 cpus=4 kernel=x\nPUB-START id=rseq-basic_test cap=300\n'
           'PUB-RESULT id=rseq-basic_test rc=0\nCT-END suite=public part=7/1 rc=0 guest_s=3\n')
    got = dict(campaign.parse(pub))
    assert got['rseq-basic_test']['kind'] == 'public' and got['rseq-basic_test']['rc'] == '0'
    assert got['rseq-basic_test']['extra'] == pub.splitlines()          # classify reads the whole boot


def test_list_file_groups_tests_by_part(tmp_path):
    img = make_image(tmp_path, 'stress', '''\
        # PART TEST
        mem1 vm.r1
        mem1 vm-rw.r1

        cpu1 mutex.r1
        ''')
    assert campaign.read_list(img, 'stress') == [('mem1', ['vm.r1', 'vm-rw.r1']), ('cpu1', ['mutex.r1'])]


# --- the harness ---

OSSL_LIST = '''\
    0/2 default:test_a
    1/2 default:test_b
    0/2 generic:test_a
    1/2 generic:test_b
    '''


def test_all_pass(setup):
    run, fake, _ = setup(OSSL_LIST, {'0/2': serial('0/2', 'CT-RESULT openssl default:test_a rc=0 ok=3 notok=0',
                                                   'CT-RESULT openssl generic:test_a rc=0 ok=3 notok=0')})
    res = run('part-0')
    assert res.outcome == 'passed', res.message
    assert subs(res) == {'default:test_a': ('passed', None), 'generic:test_a': ('passed', None)}
    assert fake.calls[0]['part'] == '0/2' and fake.calls[0]['vcpus'] == 4 and fake.calls[0]['cap'] == 60


def test_openssl_notok_fails(setup):
    run, _, _ = setup(OSSL_LIST, {'0/2': serial('0/2', 'CT-RESULT openssl default:test_a rc=0 ok=10 notok=1',
                                                'CT-RESULT openssl generic:test_a rc=0 ok=3 notok=0')})
    res = run('part-0')
    assert res.outcome == 'failed'
    assert subs(res)['default:test_a'] == ('failed', None)
    assert 'notok=1' in next(s.longrepr for s in res.sub if s.name == 'default:test_a')
    v = verdicts()
    assert v.openssl('default:test_a', {'rc': '0', 'ok': '10', 'notok': '1'}, [])[0] == 'failed'
    assert v.openssl('default:test_a', {'rc': '0', 'ok': '10', 'notok': '0'}, [])[0] == 'passed'
    out = v.openssl('x', {'rc': '1', 'ok': '2', 'notok': '1'}, ['CT-OUT x | not ok 3 - y'])
    assert out[0] == 'failed' and 'not ok 3 - y' in out[1]


def test_part_without_ct_end_fails_with_serial_tail(setup):
    noise = [f'[   {i}.0] kernel line {i}' for i in range(100)]
    text = serial('0/2', 'CT-RESULT openssl default:test_a rc=0 ok=3 notok=0', *noise, 'Kernel panic - not syncing',
                  end=False)
    run, _, _ = setup(OSSL_LIST, {'0/2': text})
    res = run('part-0')
    assert res.outcome == 'failed'
    assert 'CT-END' in res.message
    tail = text.splitlines()[-40:]
    assert '\n'.join(tail) in res.message
    assert text.splitlines()[-41] not in res.message.splitlines()
    assert subs(res)['default:test_a'] == ('passed', None)              # what did finish is still reported
    assert subs(res)['generic:test_a'] == ('failed', 'missing')


def test_listed_test_missing_from_output_fails(setup):
    run, _, _ = setup(OSSL_LIST, {'0/2': serial('0/2', 'CT-RESULT openssl default:test_a rc=0 ok=3 notok=0')})
    res = run('part-0')
    assert res.outcome == 'failed'
    assert subs(res) == {'default:test_a': ('passed', None), 'generic:test_a': ('failed', 'missing')}
    assert 'not in the output' in next(s.longrepr for s in res.sub if s.name == 'generic:test_a')


def test_unlisted_result_is_judged_and_flagged(setup):
    run, _, _ = setup(OSSL_LIST, {'0/2': serial('0/2', 'CT-RESULT openssl default:test_a rc=0 ok=3 notok=0',
                                                'CT-RESULT openssl generic:test_a rc=0 ok=3 notok=0',
                                                'CT-RESULT openssl avx2:test_z rc=0 ok=3 notok=0')})
    res = run('part-0')
    assert res.outcome == 'failed' and subs(res)['avx2:test_z'] == ('failed', None)
    assert 'not in the image' in next(s.longrepr for s in res.sub if s.name == 'avx2:test_z')


FLAKY = '''
    flaky = { "default:test_a" = "timing assertion (docs/x.md)" }
    '''


def test_flaky_test_rerun_once_then_reported_flaky(setup):
    bad = serial('0/2', 'CT-RESULT openssl default:test_a rc=1 ok=2 notok=1', 'CT-RESULT openssl generic:test_a rc=1 ok=2 notok=1')
    run, fake, _ = setup(OSSL_LIST, {'0/2': [bad, bad, bad]}, extra=FLAKY)
    res = run('part-0')
    assert len(fake.calls) == 2                                         # one rerun, not more
    assert fake.calls[1]['work'] != fake.calls[0]['work']
    s = subs(res)
    assert s['default:test_a'] == ('skipped', 'flaky')
    assert s['generic:test_a'] == ('failed', None)                      # not flaky: its first result stands
    assert res.outcome == 'failed'


def test_flaky_test_that_passes_on_the_rerun_passes(setup):
    bad = serial('0/2', 'CT-RESULT openssl default:test_a rc=1 ok=2 notok=1', 'CT-RESULT openssl generic:test_a rc=0 ok=3 notok=0')
    good = serial('0/2', 'CT-RESULT openssl default:test_a rc=0 ok=3 notok=0', 'CT-RESULT openssl generic:test_a rc=0 ok=3 notok=0')
    run, fake, _ = setup(OSSL_LIST, {'0/2': [bad, good]}, extra=FLAKY)
    res = run('part-0')
    assert len(fake.calls) == 2 and res.outcome == 'passed', res.message
    assert subs(res)['default:test_a'] == ('passed', None)


def test_flaky_test_that_passes_is_not_rerun(setup):
    good = serial('0/2', 'CT-RESULT openssl default:test_a rc=0 ok=3 notok=0', 'CT-RESULT openssl generic:test_a rc=0 ok=3 notok=0')
    run, fake, _ = setup(OSSL_LIST, {'0/2': good}, extra=FLAKY)
    assert run('part-0').outcome == 'passed' and len(fake.calls) == 1


def test_parts_use_private_work_folders(setup, tmp_path):
    texts = {'0/2': serial('0/2', 'CT-RESULT openssl default:test_a rc=0 ok=3 notok=0', 'CT-RESULT openssl generic:test_a rc=0 ok=3 notok=0'),
             '1/2': serial('1/2', 'CT-RESULT openssl default:test_b rc=0 ok=3 notok=0', 'CT-RESULT openssl generic:test_b rc=0 ok=3 notok=0')}
    run, fake, img = setup(OSSL_LIST, texts)
    before = {p: p.read_bytes() for p in img.iterdir()}
    assert run('part-0').outcome == 'passed' and run('part-1').outcome == 'passed'
    w0, w1 = fake.calls[0]['work'], fake.calls[1]['work']
    assert w0 != w1 and w0 not in w1.parents and w1 not in w0.parents
    assert (tmp_path / 'run') in w0.parents and (tmp_path / 'run') in w1.parents
    # the image is read in place, never written: both boots get the cache entry's file, unchanged
    assert fake.calls[0]['image'] == fake.calls[1]['image'] == img / 'openssl.cpio.zst'
    assert {p: p.read_bytes() for p in img.iterdir()} == before
    assert not any(img in c['work'].parents for c in fake.calls)


def test_part_count_must_match_the_image(setup):
    run, fake, _ = setup(OSSL_LIST, {'0/2': ''}, parts=3)
    res = run('part-0')
    assert res.outcome == 'failed' and '2 parts' in res.message and not fake.calls


EXPECTED = '''
    expected = { "default:test_a" = "natemu runs x87 in reduced precision (FEX) (docs/x.md)" }
    '''


def test_expected_failure_reported_as_expected_fail(setup):
    run, _, _ = setup(OSSL_LIST, {'0/2': serial('0/2', 'CT-RESULT openssl default:test_a rc=1 ok=2 notok=1',
                                                'CT-RESULT openssl generic:test_a rc=0 ok=3 notok=0')}, extra=EXPECTED)
    res = run('part-0')
    assert res.outcome == 'passed', res.message
    assert subs(res)['default:test_a'] == ('skipped', 'expected-fail')
    assert 'reduced precision' in next(s.longrepr for s in res.sub if s.name == 'default:test_a')


def test_expected_failure_that_passes_fails_the_case(setup):
    run, _, _ = setup(OSSL_LIST, {'0/2': serial('0/2', 'CT-RESULT openssl default:test_a rc=0 ok=3 notok=0',
                                                'CT-RESULT openssl generic:test_a rc=0 ok=3 notok=0')}, extra=EXPECTED)
    res = run('part-0')
    assert res.outcome == 'failed'
    assert subs(res)['default:test_a'] == ('failed', 'unexpected-pass')
    assert 'remove' in next(s.longrepr for s in res.sub if s.name == 'default:test_a')


def test_expected_entry_for_unknown_test_is_reported(setup):
    extra = '''
        expected = { "default:test_renamed" = "x87 (docs/x.md)" }
        flaky = { "generic:gone" = "timing (docs/x.md)" }
        '''
    ok0 = serial('0/2', 'CT-RESULT openssl default:test_a rc=0 ok=3 notok=0', 'CT-RESULT openssl generic:test_a rc=0 ok=3 notok=0')
    ok1 = serial('1/2', 'CT-RESULT openssl default:test_b rc=0 ok=3 notok=0', 'CT-RESULT openssl generic:test_b rc=0 ok=3 notok=0')
    run, _, _ = setup(OSSL_LIST, {'0/2': ok0, '1/2': ok1}, extra=extra)
    r0, r1 = run('part-0'), run('part-1')
    assert r0.outcome == 'failed'
    assert subs(r0)['expected:default:test_renamed'] == ('failed', 'stale-entry')
    assert subs(r0)['flaky:generic:gone'] == ('failed', 'stale-entry')
    assert r1.outcome == 'passed' and not any(c == 'stale-entry' for _, c in subs(r1).values())   # once per suite


WITHOUT_AFP = '''
    expected_without.afp = { "default:test_a" = "band-ue-flag: fast mode without FEAT_AFP (docs/x.md)" }
    '''
BAD_A = 'CT-RESULT openssl default:test_a rc=1 ok=2 notok=1', 'CT-RESULT openssl generic:test_a rc=0 ok=3 notok=0'


def test_expected_without_a_feature_applies_on_a_host_lacking_it(setup, monkeypatch):
    monkeypatch.setenv('NATEMU_TEST_HOST_FEATURES', 'afp=0')
    run, _, _ = setup(OSSL_LIST, {'0/2': serial('0/2', *BAD_A)}, extra=WITHOUT_AFP)
    res = run('part-0')
    assert res.outcome == 'passed', res.message
    assert subs(res)['default:test_a'] == ('skipped', 'expected-fail')
    assert 'band-ue-flag' in next(s.longrepr for s in res.sub if s.name == 'default:test_a')


def test_expected_without_a_feature_must_pass_on_a_host_with_it(setup, monkeypatch):
    monkeypatch.setenv('NATEMU_TEST_HOST_FEATURES', 'afp=1')
    run, _, _ = setup(OSSL_LIST, {'0/2': serial('0/2', *BAD_A)}, extra=WITHOUT_AFP)
    res = run('part-0')
    assert res.outcome == 'failed' and subs(res)['default:test_a'] == ('failed', None)


def test_expected_without_afp_does_not_apply_in_accurate_mode(setup, monkeypatch):
    monkeypatch.setenv('NATEMU_TEST_HOST_FEATURES', 'afp=0')
    monkeypatch.setenv('NATEMU_FP', 'accurate')
    run, _, _ = setup(OSSL_LIST, {'0/2': serial('0/2', *BAD_A)}, extra=WITHOUT_AFP)
    res = run('part-0')
    assert res.outcome == 'failed' and subs(res)['default:test_a'] == ('failed', None)


def test_expected_without_afp_applies_with_afp_hidden(setup, monkeypatch):
    """A host.afp = false variant on a Mac with AFP hides it (NATEMU_FEXC_FEATURE_OFF=afp, tests/features.toml): its
    expected_without.afp entries apply as on a Mac without it."""
    monkeypatch.setenv('NATEMU_TEST_HOST_FEATURES', 'afp=1')
    monkeypatch.setenv('NATEMU_FEXC_FEATURE_OFF', 'afp')
    run, _, _ = setup(OSSL_LIST, {'0/2': serial('0/2', *BAD_A)}, extra=WITHOUT_AFP)
    assert subs(run('part-0'))['default:test_a'] == ('skipped', 'expected-fail')


def test_expected_without_an_unknown_feature_fails_and_is_not_applied(setup, monkeypatch):
    extra = '''
        expected_without.apf = { "default:test_a" = "misspelt (docs/x.md)" }
        '''
    run, _, _ = setup(OSSL_LIST, {'0/2': serial('0/2', *BAD_A)}, extra=extra)
    s = subs(run('part-0'))
    assert s['expected_without.apf'] == ('failed', 'stale-entry')
    assert s['default:test_a'] == ('failed', None)


@pytest.mark.parametrize('features', ['afp=0', 'afp=1'])
def test_expected_without_entry_for_unknown_test_is_reported_on_any_host(setup, monkeypatch, features):
    extra = '''
        expected_without.afp = { "default:test_renamed" = "band-ue-flag (docs/x.md)" }
        '''
    ok0 = serial('0/2', 'CT-RESULT openssl default:test_a rc=0 ok=3 notok=0', 'CT-RESULT openssl generic:test_a rc=0 ok=3 notok=0')
    monkeypatch.setenv('NATEMU_TEST_HOST_FEATURES', features)
    run, _, _ = setup(OSSL_LIST, {'0/2': ok0}, extra=extra)
    assert subs(run('part-0'))['expected_without.afp:default:test_renamed'] == ('failed', 'stale-entry')


def test_unsupported_and_expected_is_neither_pass_nor_fail(setup):
    listing = '0/1 test-a\n0/1 test-b\n'
    text = serial('0/1', 'CT-RESULT libm test-a rc=77', 'CT-RESULT libm test-b rc=0', suite='libm')
    run, _, _ = setup(listing, {'0/1': text}, suite='libm', verdict='libm', parts=1,
                      extra='expected = { "test-a" = "x87 (docs/x.md)" }\n')
    res = run('part-0')
    assert res.outcome == 'passed', res.message
    assert subs(res) == {'test-a': ('skipped', 'unsupported'), 'test-b': ('passed', None)}


def test_ct_end_of_another_part_fails(setup):
    run, _, _ = setup(OSSL_LIST, {'0/2': serial('1/2', 'CT-RESULT openssl default:test_a rc=0 ok=3 notok=0',
                                                'CT-RESULT openssl generic:test_a rc=0 ok=3 notok=0')})
    res = run('part-0')
    assert res.outcome == 'failed' and 'part=1/2' in res.message


def test_suite_timeout_covers_a_rerun(tmp_path):
    fam, suite = make_family(tmp_path, extra=FLAKY)
    assert campaign.HARNESS.case_timeout(fam, suite, 'part-0') >= 2 * 60
    assert campaign.HARNESS.cases(fam, suite) == ['part-0', 'part-1']


def test_perf_parts_take_performance_cores_only(tmp_path):
    fam, suite = make_family(tmp_path, extra='perf_parts = ["part-1"]\n')
    assert campaign.HARNESS.case_needs(fam, suite, 'part-1').perf
    assert campaign.HARNESS.case_needs(fam, suite, 'part-0') == suite.needs and not suite.needs.perf
    pub = next(s for s in family.load(paths.repo_root() / 'tests/correctness/family.toml').suites if s.name == 'public')
    assert [c for c in campaign.HARNESS.cases(fam, pub) if campaign.HARNESS.case_needs(fam, pub, c).perf] == ['part-1', 'part-2']


def test_boot_runs_natemu_with_boot_py_command_line(tmp_path, monkeypatch):
    """boot() itself: the stub's argv as tools/correctness/boot.py builds it, a private serial file, stopped after CT-END."""
    import types
    stub = tmp_path / 'natemu'
    stub.write_text('#!/bin/sh\n'
                    'all="$0 $*"\n'
                    'while [ $# -gt 0 ]; do case $1 in --serial) s=$2;; esac; shift; done\n'
                    'printf "CT-BEGIN suite=torture part=0/1 cpus=4 kernel=x\\nCT-END suite=torture part=0/1 rc=0 guest_s=1\\n" > "$s"\n'
                    'echo "$all" > "$(dirname "$s")/argv"\n'
                    'sleep 60\n')                                   # powers off late: boot() stops it after CT-END
    stub.chmod(0o755)
    n = types.SimpleNamespace(stub=stub, runtime=tmp_path / 'runtime')
    monkeypatch.setattr(campaign.artifacts, 'natemu', lambda: n)
    monkeypatch.setattr(campaign.artifacts, 'fexc', lambda: tmp_path / 'fexc')
    monkeypatch.setattr(campaign.resources, 'fetch', lambda name: tmp_path / f'{name}')
    monkeypatch.setattr(campaign, 'AFTER_END', 0.5)
    work = tmp_path / 'w'
    import time
    t0 = time.monotonic()
    r = campaign.boot(tmp_path / 'torture.cpio.zst', 'torture', '0/1', 4, work, 30)
    assert time.monotonic() - t0 < 15
    assert r.reached and 'CT-END' in r.serial and 'after CT-END' in r.note
    argv = (work / 'argv').read_text().split()
    assert argv[1:] == ['--runtime', str(tmp_path / 'runtime'), '--fexc', str(tmp_path / 'fexc'),
                        '--kernel', str(tmp_path / 'debian-netboot-linux'), '--initrd', str(tmp_path / 'torture.cpio.zst'),
                        '--cmdline', 'console=ttyS0', 'mitigations=off', 'nokaslr', 'ct=torture', 'part=0/1',
                        '--vcpus', '4', '--serial', str(work / 'serial.txt')]


# --- the verdicts, one per campaign, on lines in the scripts' formats ---

def test_torture_verdict():
    v = verdicts()
    assert v.torture('ieee_inf-2.O0', {'rc': '0', 'out': 'd41d8cd98f00'}, [])[0] == 'passed'
    assert v.torture('ieee_inf-2.O0', {'rc': '134', 'out': 'd41d8cd98f00'}, [])[0] == 'failed'


def test_ltp_rc_classes():
    v = verdicts()
    assert v.ltp('syscalls:getcwd01', {'rc': '0', 'sec': '0.10'}, [])[0] == 'passed'
    assert v.ltp('syscalls:getcwd04', {'rc': '32', 'sec': '0.10'}, [])[0] == 'unsupported'
    r = v.ltp('syscalls:kill11', {'rc': '2', 'sec': '120.00'}, ['CT-OUT syscalls:kill11 | kill11.c:12: TBROK: timeout'])
    assert r[0] == 'failed' and 'TBROK' in r[1] and 'rc=2' in r[1]


def test_libm_77_is_unsupported():
    v = verdicts()
    assert v.libm('test-double-exp', {'rc': '0'}, [])[0] == 'passed'
    assert v.libm('test-double-libmvec-exp-avx512f', {'rc': '77'}, [])[0] == 'unsupported'
    assert v.libm('test-double-libmvec-exp-avx2', {'rc': 'skip'}, [])[0] == 'unsupported'
    assert v.libm('test-ldouble-exp', {'rc': '1'}, ['CT-OUT test-ldouble-exp | Failure: exp (0x1p+16384)'])[0] == 'failed'
    assert v.gstr('test-strlen', {'rc': '77'}, [])[0] == 'unsupported'
    assert v.gstr('test-strlen', {'rc': '0'}, [])[0] == 'passed'


def test_reference_compared_verdicts(tmp_path, monkeypatch):
    """i386 and torture32 (reference = "x86"): seeding records the reference machine's answers and never passes; a run
    passes only with the recorded output (i386: static and dynamic builds share one file) or status and hash
    (torture32: status alone for a signal, and for output that differed between two seeding runs)."""
    v = verdicts()
    monkeypatch.setattr(v, 'I386', tmp_path / 'i386'); monkeypatch.setattr(v, 'TORTURE32', tmp_path / 't32.txt')
    out = lambda n, *lines: [f'CT-LOG {n} | {ln}'.rstrip() for ln in lines] + ['CT-DMESG noise']
    monkeypatch.setenv('TESTKIT_SEED_ANSWERS', '1')
    assert v.i386('x.dyn', {'rc': '0'}, out('x.dyn', 'a', '', 'b'))[0] == 'unverified'
    assert (tmp_path / 'i386/x.txt').read_text() == 'a\n\nb\n'
    assert v.i386('x.static', {'rc': '0'}, out('x.static', 'a'))[0] == 'failed'         # differs from the dynamic build
    assert v.i386('y', {'rc': '1'}, [])[0] == 'failed'
    for _ in range(2): assert v.torture32('t.O0', {'rc': '0', 'out': 'aa'}, [])[0] == 'unverified'
    v.torture32('addr.O0', {'rc': '0', 'out': 'aa'}, []); v.torture32('addr.O0', {'rc': '0', 'out': 'bb'}, [])
    v.torture32('abort.O0', {'rc': '134', 'out': 'cc'}, [])
    assert (tmp_path / 't32.txt').read_text() == 'abort.O0 134\naddr.O0 0 *\nt.O0 0 aa\n'
    monkeypatch.delenv('TESTKIT_SEED_ANSWERS')
    assert v.i386('x.static', {'rc': '0'}, out('x.static', 'a', '', 'b'))[0] == 'passed'
    assert v.i386('x.dyn', {'rc': '0'}, out('x.dyn', 'a', '', 'c'))[0] == 'failed'
    assert v.i386('x.dyn', {'rc': '1'}, out('x.dyn', 'a', '', 'b'))[0] == 'failed'
    assert v.i386('unknown', {'rc': '0'}, [])[0] == 'failed'
    t = lambda n, rc, o: v.torture32(n, {'rc': rc, 'out': o}, [])[0]
    assert [t('t.O0', '0', 'aa'), t('t.O0', '0', 'zz'), t('t.O0', '1', 'aa'), t('new.O0', '0', 'aa')] == ['passed', 'failed', 'failed', 'failed']
    assert [t('addr.O0', '0', 'zz'), t('addr.O0', '1', 'zz'), t('abort.O0', '134', 'zz'), t('abort.O0', '0', 'cc')] == ['passed', 'failed', 'passed', 'failed']


def test_reference_status_verdicts(tmp_path, monkeypatch):
    """ltp32 and libm32: the suite's own verdict from the exit status against the x86 reference machine's status,
    recorded when seeding (`*` where two seeding runs differ): a failure with the reference's own status passes (as
    torture32), another fails, and unsupported here where the reference passed fails."""
    v = verdicts()
    monkeypatch.setattr(v, 'LTP32', tmp_path / 'ltp32.txt'); monkeypatch.setattr(v, 'LIBM32', tmp_path / 'libm32.txt')
    monkeypatch.setenv('TESTKIT_SEED_ANSWERS', '1')
    for name, rc in (('s:a', '0'), ('s:b', '32'), ('s:c', '2'), ('s:t', '0'), ('s:t', '1'), ('s:a', '0')):
        assert v.ltp32(name, {'rc': rc}, [])[0] == 'unverified'
    assert v.libm32('math/test-x', {'rc': '0'}, [])[0] == 'unverified'
    assert (tmp_path / 'ltp32.txt').read_text() == 's:a 0\ns:b 32\ns:c 2\ns:t *\n'
    monkeypatch.delenv('TESTKIT_SEED_ANSWERS')
    t = lambda n, rc: v.ltp32(n, {'rc': rc}, [])[0]
    assert [t('s:a', '0'), t('s:a', '32'), t('s:a', '1'), t('s:b', '32'), t('s:c', '2'), t('s:c', '1'), t('s:t', '32')] == \
        ['passed', 'failed', 'failed', 'unsupported', 'passed', 'failed', 'unsupported']
    assert 'fails the same way' in v.ltp32('s:c', {'rc': '2'}, [])[1]
    assert 'reference machine: rc=2' in v.ltp32('s:c', {'rc': '1'}, [])[1]
    assert t('s:new', '0') == 'failed'
    m = lambda rc: v.libm32('math/test-x', {'rc': rc, 'fails': '0', 'fp': 'x'}, [], mode={'afp': True, 'fp': 'fast'})[0]
    assert [m('0'), m('77')] == ['passed', 'failed']


def test_libm32_failure_must_match_its_recorded_one(tmp_path, monkeypatch):
    """libm32: a failing program must fail exactly as recorded for its floating-point mode (count and hash of its
    failure lines, libm32-fails-MODE.txt), else `changed`, which fails even when `expected` lists the program."""
    v = verdicts()
    monkeypatch.setattr(v, 'LIBM32', tmp_path / 'libm32.txt'); monkeypatch.setattr(v, 'LIBM32_FAILS', tmp_path)
    (tmp_path / 'libm32.txt').write_text('math/test-x 0\n')
    f = lambda fp: {'rc': '1', 'fails': '3', 'fp': fp}
    afp, noafp, acc = {'afp': True, 'fp': 'fast'}, {'afp': False, 'fp': 'fast'}, {'afp': False, 'fp': 'accurate'}
    assert v.libm32('math/test-x', f('aa'), [], mode=afp)[0] == 'changed'           # nothing recorded
    monkeypatch.setenv('TESTKIT_SEED_FAILURES', '1')                         # recording: never a pass or an expected fail
    assert v.libm32('math/test-x', f('aa'), [], mode=afp)[0] == 'unverified'
    assert v.libm32('math/test-x', f('bb'), [], mode=noafp)[0] == 'unverified'
    assert campaign.adjust('math/test-x', 'unverified', 'm', {'math/test-x': 'why'}).category == 'unverified'
    assert (tmp_path / 'libm32-fails-afp.txt').read_text() == 'math/test-x fails=3 fp=aa\n'
    assert (tmp_path / 'libm32-fails-noafp.txt').read_text() == 'math/test-x fails=3 fp=bb\n'
    monkeypatch.delenv('TESTKIT_SEED_FAILURES')
    assert v.libm32('math/test-x', f('aa'), [], mode=afp)[0] == 'failed'
    assert v.libm32('math/test-x', f('aa'), [], mode=noafp)[0] == 'changed'
    assert v.libm32('math/test-x', f('aa'), [], mode=acc)[0] == 'changed'          # libm32-fails-noafp-accurate.txt
    assert v.libm32('math/test-x', {'rc': '1'}, [], mode=afp)[0] == 'changed'        # no fingerprint on the line
    assert campaign.adjust('math/test-x', 'changed', 'm', {'math/test-x': 'why'}).outcome == 'failed'
    monkeypatch.setenv('TESTKIT_SEED_FAILURES', '1')                         # a second recording that differs: fp=*
    v.libm32('math/test-x', f('cc'), [], mode=afp)
    monkeypatch.delenv('TESTKIT_SEED_FAILURES')
    assert (tmp_path / 'libm32-fails-afp.txt').read_text() == 'math/test-x fp=*\n'
    assert v.libm32('math/test-x', f('dd'), [], mode=afp)[0] == 'failed'


def _run_libm(tmp_path, progs):
    """tools/correctness/run-libm.sh over canned programs {name: (output bytes, exit status)}: {name: fields}."""
    import hashlib, os, subprocess
    d = tmp_path / f'libm-{hashlib.md5(repr(progs).encode()).hexdigest()[:8]}'
    (d / 'bin').mkdir(parents=True)
    (d / 'bin' / 'timeout').write_text('#!/bin/sh\nshift 3\nexec "$@"\n'); (d / 'bin' / 'timeout').chmod(0o755)
    rows = []
    for i, (name, (out, rc)) in enumerate(progs.items()):
        (d / f'{i}.out').write_bytes(out)
        rows.append(f'{name}\t{d / "cwd"}\tsh -c \'cat {d / f"{i}.out"}; exit {rc}\'\n')
    (d / 'libm-cmds.txt').write_text(''.join(rows))
    env = dict(os.environ, PATH=f'{d / "bin"}:{os.environ["PATH"]}', TMPDIR=str(d))
    r = subprocess.run(['sh', str(REPO / 'tools/correctness/run-libm.sh'), str(d)], capture_output=True, env=env, check=True)
    return {w[2]: campaign._kv(w[3:]) for w in (ln.split() for ln in r.stdout.decode().splitlines()) if w[:2] == ['CT-RESULT', 'libm']}


def test_run_libm_fingerprints_the_whole_output_and_status(tmp_path):
    """run-libm.sh's fp= is the md5 prefix of a failing program's whole output followed by `rc=EXIT`: a case that fails
    by more (NaN for a few ulp) or a changed exit status changes it, a NUL byte in the output does not put the script's
    pid into it, and only test-nan-payload's long double lines are left out."""
    import hashlib
    base = b'testing double\nFailure: Test: cos (0x1p+0)\nResult:\n is:          1.0000000000000002e+00\n' \
           b' should be:   1.0000000000000000e+00\nMaximal error of `cos\'\n is      : 1 ulp\n'
    worse = base.replace(b'1.0000000000000002e+00', b'nan')
    nul = b'Failure: Test: x\n\x00\n'
    got = _run_libm(tmp_path, {'math/a': (base, 1), 'math/b': (worse, 1), 'math/c': (base, 2), 'math/d': (nul, 1),
                               'math/e': (b'Segmentation fault\n', 139), 'math/p': (base, 0), 'math/u': (base, 77)})
    assert got['math/a'] == {'rc': '1', 'fails': '1', 'fp': hashlib.md5(base + b'rc=1\n').hexdigest()[:12]}
    assert got['math/b']['fp'] != got['math/a']['fp'] and got['math/b']['fails'] == '1'          # a value changed
    assert got['math/c']['fp'] != got['math/a']['fp']                                            # the status changed
    assert got['math/e'] == {'rc': '139', 'fails': '0', 'fp': hashlib.md5(b'Segmentation fault\nrc=139\n').hexdigest()[:12]}
    assert got['math/p'] == {'rc': '0'} and got['math/u'] == {'rc': '77'}
    again = _run_libm(tmp_path, {'math/d': (nul, 1), 'math/a': (base, 1)})                      # another pid
    assert again['math/d'] == got['math/d'] == {'rc': '1', 'fails': '1', 'fp': hashlib.md5(nul + b'rc=1\n').hexdigest()[:12]}
    nan = lambda *lines: (b''.join(ln + b'\n' for ln in lines), 1)
    p = 'math/test-nan-payload'
    one = _run_libm(tmp_path, {p: nan(b'PASS: float n123', b'FAIL: long double n123 = n123', b'FAIL: _Float64x payload')})[p]
    two = _run_libm(tmp_path, {p: nan(b'PASS: float n123', b'PASS: long double n123 = n123', b'PASS: _Float64x payload')})[p]
    three = _run_libm(tmp_path, {p: nan(b'FAIL: float n123', b'FAIL: long double n123 = n123', b'FAIL: _Float64x payload')})[p]
    assert one['fp'] == two['fp'] == hashlib.md5(b'PASS: float n123\nrc=1\n').hexdigest()[:12] and three['fp'] != one['fp']
    other = _run_libm(tmp_path, {'math/test-x': nan(b'PASS: float n123', b'FAIL: long double n123 = n123')})['math/test-x']
    assert other['fp'] != one['fp']                                     # the filter is test-nan-payload's alone


def test_answers_naming_no_test_of_the_image_are_stale(setup, tmp_path):
    run, _, _ = setup(OSSL_LIST, {'0/2': serial('0/2', 'CT-RESULT openssl default:test_a rc=0 ok=1 notok=0',
                                                'CT-RESULT openssl generic:test_a rc=0 ok=1 notok=0')},
                      extra='answers = ["a.txt"]\n')
    (tmp_path / 'tests' / 'c' / 'a.txt').write_text('default:test_a 0\nold:test 2\n')
    s = subs(run('part-0'))
    assert s['answers:a.txt:old:test'] == ('failed', 'stale-entry') and 'answers:a.txt:default:test_a' not in s


def test_engine_fp_variant_boots_with_natemu_fp_and_tells_the_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv('NATEMU_TEST_HOST_FEATURES', 'afp=1')
    monkeypatch.setenv('NATEMU_FEXC_FEATURE_OFF', 'afp')
    fam, suite = make_family(tmp_path)
    img = make_image(tmp_path, suite.name, OSSL_LIST)
    monkeypatch.setattr(campaign, 'image', lambda name: img)
    fake = FakeBoot({'0/2': serial('0/2', *BAD_A)})
    monkeypatch.setattr(campaign, 'boot', fake)
    seen = []
    monkeypatch.setattr(campaign, '_verdict', lambda *a: lambda name, f, extra, mode: (seen.append(mode), ('passed', ''))[1])
    variant = family.Variant((('engine.fp', 'accurate'), ('machine.vcpus', 4)))
    campaign.HARNESS.run_part(fam, suite, 'part-0', variant, tmp_path / 'run')
    assert fake.calls[0]['env'] == {'NATEMU_FP': 'accurate'} and seen[0] == {'afp': False, 'fp': 'accurate'}


def test_expected_without_afp_kept_in_accurate_mode_by_its_deviation(setup, monkeypatch):
    """In accurate mode with AFP hidden, an expected_without.afp entry applies only when its reason names a deviation
    accurate mode keeps (x87-band-ue-flag); a band-ue-flag one must pass."""
    monkeypatch.setenv('NATEMU_TEST_HOST_FEATURES', 'afp=0')
    monkeypatch.setenv('NATEMU_FP', 'accurate')
    extra = '''
        expected_without.afp = { "default:test_a" = "x87-band-ue-flag: x87 (docs/x.md)", "generic:test_a" = "band-ue-flag: sse (docs/x.md)" }
        '''
    run, _, _ = setup(OSSL_LIST, {'0/2': serial('0/2', 'CT-RESULT openssl default:test_a rc=1 ok=2 notok=1',
                                                'CT-RESULT openssl generic:test_a rc=1 ok=2 notok=1')}, extra=extra)
    s = subs(run('part-0'))
    assert s['default:test_a'] == ('skipped', 'expected-fail') and s['generic:test_a'] == ('failed', None)


def test_libm32_accurate_variant_only_without_afp():
    rules = family._load_module(REPO / 'tests' / 'correctness' / 'rules.py', 'test_correctness_rules')
    v = lambda **p: family.Variant(tuple(sorted({'machine.vcpus': 4, **p}.items())))
    assert rules.applies('libm32', 'part-0', v(**{'host.afp': True, 'engine.fp': 'accurate'}), {})
    assert rules.applies('libm32', 'part-0', v(**{'host.afp': False, 'engine.fp': 'accurate'}), {}) is None
    assert rules.applies('libm32', 'part-0', v(**{'host.afp': True, 'engine.fp': 'fast'}), {}) is None


def test_native_vm_boots_the_same_kernel_and_command_line_under_kvm(tmp_path, monkeypatch):
    """native_vm(): the kernel and image go to the x86 machine in one tar, and QEMU there boots them under KVM with
    boot()'s command line, vCPUs and 2 GiB, in a named container the session's end kills; the serial output is the
    result and the machine's stderr lines its provenance."""
    import subprocess, tarfile, types
    (tmp_path / 'k').write_bytes(b'kernel'); (tmp_path / 'i.cpio.zst').write_bytes(b'initrd')
    seen = {}

    def fake(cmd, stdin, capture_output, timeout):
        seen['cmd'] = cmd
        with tarfile.open(fileobj=stdin) as t: seen['files'] = {m.name: t.extractfile(m).read() for m in t}
        return types.SimpleNamespace(returncode=0, stdout=serial('1/4', 'CT-RESULT ltp s:a rc=0', suite='ltp').encode(),
                                     stderr=b'ref-cpu: AMD X\nref-kernel: 7.0\nref-boot: KVM\nref-qemu: QEMU emulator version 10\n')
    monkeypatch.setattr(subprocess, 'run', fake)
    r = campaign.native_vm(tmp_path / 'i.cpio.zst', 'ltp', '1/4', 4, 'u@x86', 600, tmp_path / 'w', kernel=tmp_path / 'k')
    assert r.reached and seen['files'] == {'kernel': b'kernel', 'initrd': b'initrd'}
    assert seen['cmd'][:3] == ['ssh', '-o', 'BatchMode=yes'] and seen['cmd'][3] == 'u@x86'
    script = seen['cmd'][4]
    name = re.search(r'--name (\S+)', script)[1]
    assert re.fullmatch(r'natemu-ref-ltp-[0-9a-f]{12}-1-4', name)        # random, not a pid: two Macs never clash
    for want in ('--device /dev/kvm', f'-enable-kvm -cpu {campaign.REFERENCE_CPU} -m 2048 -smp 4', 'ct=ltp part=1/4',
                 campaign.artifacts.BENCH_BUILDER, f"trap 'docker kill {name}", 'EXIT HUP TERM INT',
                 f'timeout {campaign.APT_TIMEOUT} apt-get', f'archive/debian/{campaign._snap()}/'):
        assert want in script, want
    assert r.reference == {'cpu': 'AMD X', 'kernel': '7.0', 'boot': 'KVM', 'qemu': 'QEMU emulator version 10'}
    assert (tmp_path / 'w' / 'native.txt').read_text().count('CT-RESULT') == 1 and not (tmp_path / 'w' / 'boot.tar').exists()
    img = tmp_path / 'img'; img.mkdir(); (img / 'manifest.json').write_text('{"image": {"sha256": "ab"}}')
    campaign.write_reference(tmp_path, 'ltp32', img, r)
    assert (tmp_path / 'ltp32-reference.txt').read_text().splitlines()[1:] == \
        ['boot: KVM', 'cpu: AMD X', 'kernel: 7.0', 'qemu: QEMU emulator version 10', 'image: sha256 ab, cache entry img']


def test_reference_snapshot_is_the_recipes_one():
    assert campaign._snap() == re.search(r'^SNAP=(\S+)$', (REPO / 'tools/correctness/recipes/common.sh').read_text(), re.M)[1]


def test_x86st_fail_count_fails():
    v = verdicts()
    f = lambda **kw: dict({'rc': '0', 'ok': '4', 'fail': '0', 'skip': '0', 'out': '0123456789ab'}, **kw)
    assert v.x86st('sigreturn_64', f(), [])[0] == 'passed'
    assert v.x86st('sigreturn_64', f(fail='1'), ['CT-LOG sigreturn_64 | [FAIL] bad'])[0] == 'failed'
    assert v.x86st('sigreturn_64', f(rc='1'), [])[0] == 'failed'
    assert v.x86st('amx_64', f(rc='4', ok='0', skip='1'), [])[0] == 'unsupported'
    assert v.x86st('lam_64', f(ok='0', skip='3'), [])[0] == 'unsupported'
    assert v.x86st('mov_ss_trap_64', {'ct_skip': True}, [])[0] == 'unsupported'


def test_cpy_case_status_parsed():
    v = verdicts()
    assert v.cpyt('test_os', {'cases': '10', 'pass': '9', 'fail': '0', 'error': '0', 'skip': '1'}, [])[0] == 'passed'
    r = v.cpyt('test_os', {'cases': '10', 'pass': '8', 'fail': '1', 'error': '1', 'skip': '0'},
               ["CT-CASE test.test_os.TimerfdTests.test_timerfd_poll (x='a b') fail", 'CT-CASE test.test_os.X.test_y error'])
    assert r[0] == 'failed' and "test.test_os.TimerfdTests.test_timerfd_poll (x='a b') fail" in r[1]
    assert v.cpyt('test_winreg', {'cases': '3', 'pass': '0', 'fail': '0', 'error': '0', 'skip': '3'}, [])[0] == 'unsupported'
    assert v.cpyb('make', {'rc': '0', 'sec': '300.1'}, [])[0] == 'passed'
    assert v.cpyb('make', {'rc': '2', 'sec': '300.1'}, [])[0] == 'failed'


def test_stress_dmesg_oops_fails():
    v = verdicts()
    ok = {'rc': '0', 'n': '4', 'pass': '4', 'skip': '0', 'fail': '0', 'bogo': '100', 'sec': '12'}
    assert v.stress('vm.r1', ok, ['CT-DMESG [ 1.0] Linux version 6.12.107'])[0] == 'passed'
    r = v.stress('vm.r1', ok, ['CT-DMESG [ 9.0] Oops: 0000 [#1] PREEMPT SMP'])
    assert r[0] == 'failed' and 'Oops' in r[1]
    assert v.stress('vm.r1', dict(ok, fail='2'), [])[0] == 'failed'
    assert v.stress('vm.r1', dict(ok, rc='2'), [])[0] == 'failed'
    r = v.stress('vm.r1', dict(ok, sec='240'), [])                                  # ran to the timeout: noted, not failed
    assert r[0] == 'passed' and 'timeout' in r[1]                                   # (plan, format decision 2)
    # review: incomplete lines failed (stress-check.py compared pass/skip with a reference run; nothing does now)
    assert v.stress('vm.r1', {'rc': '0'}, [])[0] == 'failed'
    assert v.stress('vm.r1', dict(ok, **{'pass': '-', 'skip': '-'}), [])[0] == 'failed'
    assert v.stress('vm.r1', dict(ok, n='4', **{'pass': '2', 'skip': '1'}), [])[0] == 'failed'
    assert v.cpyt('test_os', {'cases': '5'}, [])[0] == 'failed'
    oom = 'CT-DMESG [ 5.0] Out of memory: Killed process 77 (stress-ng-vm)'
    assert v.stress('vm.r1', ok, [oom])[0] == 'passed'                             # what the stressor asks for


ORACLE = '''Test SB Allowed
States 3
0:EAX=0; 1:EAX=1;
0:EAX=1; 1:EAX=0;
0:EAX=1; 1:EAX=1;
No
Witnesses
Positive: 0 Negative: 3
Condition exists (0:EAX=0 /\\ 1:EAX=0)
Observation SB Never 0 3
'''


@pytest.fixture
def litmus_image(tmp_path):
    (tmp_path / 'oracle').mkdir()
    (tmp_path / 'oracle' / 'x86.txt').write_text(ORACLE)
    return tmp_path


def test_litmus_forbidden_state_fails(litmus_image):
    v = verdicts()
    log = ['CT-LOG x86.SB | Histogram (4 states)',
           'CT-LOG x86.SB | 499990:>0:EAX=1; 1:EAX=0;', 'CT-LOG x86.SB | 5     *>0:EAX=0; 1:EAX=0;',
           'CT-LOG x86.SB | 5     :>0:EAX=1; 1:EAX=1;', 'CT-LOG x86.SB | 0:>0:EAX=0; 1:EAX=1;',
           'CT-LOG x86.SB | Positive: 5, Negative: 499995']
    r = v.litmus('x86.SB', {'rc': '0', 'obs': 'Sometimes', 'sec': '3'}, log, image=litmus_image)
    assert r[0] == 'failed' and 'FORBIDDEN' in r[1] and '0:eax=0; 1:eax=0 x5' in r[1]


def test_litmus_incomplete_histogram_fails(litmus_image):
    """A lost or garbled histogram line could hide a forbidden state: the state lines must number litmus7's
    `Histogram (N states)` and their counts sum to Positive + Negative."""
    v = verdicts()
    f = {'rc': '0', 'obs': 'Never', 'sec': '3'}
    head, tail = 'CT-LOG x86.SB | Histogram (2 states)', 'CT-LOG x86.SB | Positive: 0, Negative: 500000'
    good = ['CT-LOG x86.SB | 499990:>0:EAX=1; 1:EAX=0;', 'CT-LOG x86.SB | 10    :>0:EAX=1; 1:EAX=1;']
    assert v.litmus('x86.SB', f, [head, *good, tail], image=litmus_image)[0] == 'passed'
    r = v.litmus('x86.SB', f, [head, good[0], 'CT-LOG x86.SB | 10 :?0:EAX=0; 1:EAX', tail], image=litmus_image)
    assert r[0] == 'failed' and '1 state lines, the histogram declares 2' in r[1] and 'sum to 499990' in r[1]
    r = v.litmus('x86.SB', f, [head, good[0], 'CT-LOG x86.SB | 1    :>0:EAX=1; 1:EAX=1;', tail], image=litmus_image)
    assert r[0] == 'failed' and 'sum to 499991, Positive + Negative = 500000' in r[1]
    assert 'no "Histogram (N states)" line' in v.litmus('x86.SB', f, [*good, tail], image=litmus_image)[1]
    assert 'no "Positive: P, Negative: Q" line' in v.litmus('x86.SB', f, [head, *good], image=litmus_image)[1]


def test_litmus_unseen_allowed_state_passes_with_note(litmus_image):
    v = verdicts()
    log = ['CT-LOG x86.SB | Histogram (2 states)', 'CT-LOG x86.SB | 499990:>0:EAX=1; 1:EAX=0;',
           'CT-LOG x86.SB | 10    :>0:EAX=1; 1:EAX=1;', 'CT-LOG x86.SB | Positive: 0, Negative: 500000']
    r = v.litmus('x86.SB', {'rc': '0', 'obs': 'Never', 'sec': '3'}, log, image=litmus_image)
    assert r[0] == 'passed' and '1 of 3 allowed states never observed' in r[1]
    assert v.litmus('x86.SB', {'rc': '1', 'obs': '', 'sec': '3'}, log, image=litmus_image)[0] == 'failed'
    assert v.litmus('x86.SB', {'rc': '0', 'obs': 'Never', 'sec': '3'}, [], image=litmus_image)[0] == 'failed'
    assert v.litmus('x86.MP', {'rc': '0', 'obs': 'Never', 'sec': '3'}, log, image=litmus_image)[0] == 'failed'   # no oracle
    assert v.litmus('x86.SB+x', {'ct_skip': True, 'threads': '8'}, [], image=litmus_image)[0] == 'unsupported'


def pub(id_, rc, part='3/1', body=()):
    return [f'CT-BEGIN suite=public part={part} cpus=4 kernel=x', f'PUB-START id={id_} cap=300', *body,
            f'PUB-RESULT id={id_} rc={rc}', f'CT-END suite=public part={part} rc={rc} guest_s=3']


def test_public_needs_review_is_unverified():
    """A row that ran to the end without its program's success signal is unverified, never passed."""
    v = verdicts()
    for name in ('ltp-mmap01', 'kunit-memcpy_kunit', 'kernel-test_bitmap', 'locktorture-spin_lock', 'rcutorture-rcu',
                 'kernel-unknown_module'):
        assert v.public(name, {'rc': '0'}, pub(name, 0))[0] == 'unverified', name
    assert v.public('rseq-basic_test', {'rc': '4'}, pub('rseq-basic_test', 4))[0] == 'unsupported'
    assert v.public('rseq-basic_test', {'rc': '1'}, pub('rseq-basic_test', 1))[0] == 'failed'
    assert v.public('rseq-basic_test', {'rc': '124'}, pub('rseq-basic_test', 124))[0] == 'failed'     # guest cap
    assert v.public('rseq-basic_test', {'rc': '0'}, pub('rseq-other', 0))[0] == 'failed'             # identity
    assert v.public('ltp-mmap01', {'rc': '32'}, pub('ltp-mmap01', 32))[0] == 'unsupported'
    assert v.public('ltp-mmap01', {'rc': '2'}, pub('ltp-mmap01', 2))[0] == 'failed'                  # broken
    r = v.public('mm-cow', {'rc': '0'}, pub('mm-cow', 0, body=['not ok 3 cow on fork']))
    assert r[0] == 'failed' and 'failure' in r[1]


def test_public_success_signal_passes():
    """Review: a row passes on its program's own success signal (and only then)."""
    v = verdicts()
    ok = {
        'rseq-basic_test': [], 'qemu-cmpxchg': [], 'ptrace-get_syscall_info': ['ok 1 global.get_syscall_info'],
        'ltp-mmap01': ['Summary:', 'passed   1', 'failed   0', 'broken   0', 'skipped  0', 'warnings 0'],
        'kunit-memcpy_kunit': ['[   11.99]     ok 2 memcpy_test', '[   15.87] # memcpy: pass:6 fail:0 skip:0 total:6',
                               '[   15.88] ok 1 memcpy'],
        'kernel-atomic64_test': ['[    3.10] atomic64_test: passed for x86_64 platform with CX8 and with SSE'],
        'kernel-test_bitmap': ['[   12.84] test_bitmap: all 390447 tests passed'],
        'locktorture-spin_lock': ['[   42.69] spin_lock-torture:--- End of test: SUCCESS: nwriters_stress=2'],
        'rcutorture-srcu-fwd': ['[  100.1] srcu-torture:--- End of test: SUCCESS: nreaders=3'],
    }
    for name, body in ok.items():
        assert v.public(name, {'rc': '0'}, pub(name, 0, body=body))[0] == 'passed', name
    # only an indented (subtest) ok: not the suite's result
    assert v.public('kunit-memcpy_kunit', {'rc': '0'}, pub('kunit-memcpy_kunit', 0, body=['[ 1.0]     ok 2 memcpy_test']))[0] == 'unverified'
    # LTP with a failed count: rc says it too, but the summary alone must not pass it
    assert v.public('ltp-mmap01', {'rc': '0'}, pub('ltp-mmap01', 0, body=['failed   1', 'broken   0']))[0] == 'unverified'


def test_public_kernel_splat_fails_whatever_rc():
    """Review D1: a kernel failure shown only as text (an Oops in a kthread, a BUG, an RCU stall, a failed torture run,
    a stack trace) fails the row, although the test exited 0 and printed its success line."""
    v = verdicts()
    for splat in ('[ 31.2] Oops: general protection fault, probably for non-canonical address 0xdead: 0000 [#1] SMP',
                  '[ 31.2] kernel BUG at kernel/locking/locktorture.c:123!',
                  '[ 31.2] rcu: INFO: rcu_preempt detected stalls on CPUs/tasks:',
                  '[ 31.2] rcu: INFO: rcu_preempt self-detected stall on CPU',
                  '[ 31.2] rcu-torture:--- End of test: FAILURE: nreaders=3',
                  '[ 31.2] Call Trace:'):
        body = [splat, '[ 42.69] spin_lock-torture:--- End of test: SUCCESS: nwriters_stress=2']
        r = v.public('locktorture-spin_lock', {'rc': '0'}, pub('locktorture-spin_lock', 0, body=body))
        assert r[0] == 'failed' and splat in r[1], splat
        assert v.public('rseq-basic_test', {'rc': '0'}, pub('rseq-basic_test', 0, body=[splat]))[0] == 'failed', splat


def test_public_vmalloc_judged_by_its_summaries():
    """Review D2: test_vmalloc always ends -EAGAIN (modprobe exits 1), so its row is judged by its Summary: lines."""
    v = verdicts()
    good = ['[ 5.1] Summary: fix_size_alloc_test passed: 1 failed: 0 repeat: 1 loops: 100 avg: 120 usec',
            '[ 5.2] Summary: full_fit_alloc_test passed: 1 failed: 0 repeat: 1 loops: 100 avg: 99 usec']
    name = 'vmalloc-normal-cases'
    assert v.public(name, {'rc': '1'}, pub(name, 1, body=good))[0] == 'passed'
    bad = good + ['[ 5.3] Summary: long_busy_list_alloc_test passed: 0 failed: 1 repeat: 1 loops: 100 avg: 9 usec']
    r = v.public(name, {'rc': '1'}, pub(name, 1, body=bad))
    assert r[0] == 'failed' and 'long_busy_list_alloc_test' in r[1]
    assert v.public(name, {'rc': '1'}, pub(name, 1))[0] == 'failed'                                   # no summary
    assert v.public(name, {'rc': '1'}, pub(name, 1, body=good + ['[ 5.4] Call Trace:']))[0] == 'failed'
    assert v.public(name, {'rc': '124'}, pub(name, 124, body=good))[0] == 'failed'                  # guest cap
    assert v.public(name, {'rc': '1'}, pub('vmalloc-other', 1, body=good))[0] == 'failed'           # identity
    family.load.cache_clear()
    assert name not in family.load(REPO / 'tests' / 'correctness' / 'family.toml').suite('public').extra.get('expected', {})


def test_public_smoke_run_serials_pass(tmp_path):
    """The six smoke rows as they printed on the M3 (run 20261119-000536-6f13), cut to the lines the verdict reads:
    each passes on its own success signal."""
    v = verdicts()
    boot = ['[    0.0] Linux version 6.12.107-natemu-public', '[    1.0] Run /init as init process']
    rows = {
        ('0/1', 'rseq-basic_test'): ['testing current cpu', 'testing thread-local'],
        ('24/1', 'qemu-cmpxchg'): [],
        ('62/1', 'ltp-mmap01'): ['mmap01.c:84: TPASS: Functionality of mmap() successful', 'Summary:', 'passed   1',
                                 'failed   0', 'broken   0', 'skipped  0', 'warnings 0'],
        ('72/1', 'kunit-memcpy_kunit'): ['[    3.0] KTAP version 1', '[    3.0] 1..1', '[    4.4]     ok 3 memcpy_large_test',
                                         '[    4.4] # memcpy: pass:6 fail:0 skip:0 total:6', '[    4.478105] ok 1 memcpy'],
        ('75/1', 'kernel-test_bitmap'): ['[   12.846749] test_bitmap: all 390447 tests passed'],
        ('77/1', 'locktorture-spin_lock'): ['[   42.693534] Writes:  Total: 4894361  Max/Min: 2449401/2444960   Fail: 0 ',
                                            '[   42.696749] spin_lock-torture:--- End of test: SUCCESS: acq_writer_lim=0'],
    }
    for (part, name), body in rows.items():
        lines = boot + pub(name, 0, part=part, body=body)
        assert v.public(name, {'rc': '0'}, lines)[0] == 'passed', name


LKMM_ORACLE = '''Test SB+poonceonces Allowed
States 4
0:r0=0; 1:r0=0;
0:r0=0; 1:r0=1;
0:r0=1; 1:r0=0;
0:r0=1; 1:r0=1;
Ok
Witnesses
Positive: 1 Negative: 3
Condition exists (0:r0=0 /\\ 1:r0=0)
Observation SB+poonceonces Sometimes 1 3
Time SB+poonceonces 0.01
Hash=d1ed1c3c4ecd4ec6c1bf7e1a1e6b2c54

Test SB+fencembonceonces Allowed
States 3
0:r0=0; 1:r0=1;
0:r0=1; 1:r0=0;
0:r0=1; 1:r0=1;
No
Witnesses
Positive: 0 Negative: 3
Condition exists (0:r0=0 /\\ 1:r0=0)
Observation SB+fencembonceonces Never 0 3
Time SB+fencembonceonces 0.01

Test CoWW+poonceonce Allowed
States 1
[x]=2;
No
Witnesses
Positive: 0 Negative: 1
Condition exists ([x]=1)
Observation CoWW+poonceonce Never 0 1

Test MP+onceassign+derefonce Allowed
States 2
1:r0=x; 1:r1=1;
1:r0=y; 1:r1=0;
No
Witnesses
Positive: 0 Negative: 2
Condition exists (1:r0=x /\\ 1:r1=0)
Observation MP+onceassign+derefonce Never 0 2
'''
LKMM_TESTS = '''litmus003 1 1000 10 CoWW+poonceonce
litmus014 2 1000 10 MP+onceassign+derefonce
litmus026 2 1000 10 SB+fencembonceonces
litmus027 2 1000 10 SB+poonceonces
'''


@pytest.fixture
def lkmm_image(tmp_path):
    (tmp_path / 'oracle').mkdir()
    (tmp_path / 'oracle' / 'lkmm.txt').write_text(LKMM_ORACLE)
    (tmp_path / 'oracle' / 'lkmm-tests.txt').write_text(LKMM_TESTS)
    return tmp_path


def lkmm(test, histogram, cpus=4, positive=None, total=None, kind='Allowed'):
    """klitmus7's /proc/litmus output for `test` (histogram: [(count, '*' or ':', state)]) in one public boot."""
    pos = sum(c for c, s, _ in histogram if s == '*') if positive is None else positive
    tot = sum(c for c, _, _ in histogram) if total is None else total
    body = ['[    4.1] litmus027: loading out-of-tree module taints kernel.', f'Test {test} {kind}',
            f'Histogram ({len(histogram)} states)', *(f'{c:<8}{s}>{st}' for c, s, st in histogram),
            'Ok' if pos else 'No', '', 'Witnesses', f'Positive: {pos}, Negative: {tot - pos}',
            f'Condition exists (0:r0=0 /\\ 1:r0=0) is {"" if pos else "NOT "}validated',
            f'Observation {test} {"Sometimes" if pos else "Never"} {pos} {tot - pos}', f'Time {test} 0.02', '']
    lines = pub('lkmm-' + test, 0, part='115/1', body=body)
    lines[0] = lines[0].replace('cpus=4', f'cpus={cpus}')
    return lines


def test_public_lkmm_allowed_states_pass(lkmm_image):
    """An LKMM row passes when its histogram is whole, its samples are the module's (instances x size x runs) and every
    observed state is one herd7 allows; the relaxed SB outcome is allowed under the LKMM, an unseen state only noted."""
    v = verdicts()
    sb = [(30, '*', '0:r0=0; 1:r0=0;'), (9970, ':', '0:r0=0; 1:r0=1;'), (10000, ':', '0:r0=1; 1:r0=0;')]
    r = v.public('lkmm-SB+poonceonces', {'rc': '0'}, lkmm('SB+poonceonces', sb), image=lkmm_image)
    assert r[0] == 'passed', r
    assert '20000 samples (2 x 1000 x 10), 3 states' in r[1] and '1 of 4 allowed states never observed' in r[1]
    assert 'herd7 Ok, observed Sometimes' in r[1]
    mp = [(19990, ':', '1:r0=x; 1:r1=1;'), (10, ':', '1:r0=y; 1:r1=0;')]          # pointer values: location names
    assert v.public('lkmm-MP+onceassign+derefonce', {'rc': '0'}, lkmm('MP+onceassign+derefonce', mp),
                    image=lkmm_image)[0] == 'passed'
    coww = [(40000, ':', 'x=2;')]                       # one thread: four instances at 4 vCPUs, one at 1
    assert v.public('lkmm-CoWW+poonceonce', {'rc': '0'}, lkmm('CoWW+poonceonce', coww), image=lkmm_image)[0] == 'passed'
    assert v.public('lkmm-CoWW+poonceonce', {'rc': '0'}, lkmm('CoWW+poonceonce', [(10000, ':', 'x=2;')], cpus=1),
                    image=lkmm_image)[0] == 'passed'


def test_public_lkmm_forbidden_state_fails(lkmm_image):
    """A state herd7's LKMM forbids fails the row, however rarely it was seen (the judge is never weakened)."""
    v = verdicts()
    sb = [(1, '*', '0:r0=0; 1:r0=0;'), (9999, ':', '0:r0=0; 1:r0=1;'), (10000, ':', '0:r0=1; 1:r0=0;')]
    r = v.public('lkmm-SB+fencembonceonces', {'rc': '0'}, lkmm('SB+fencembonceonces', sb), image=lkmm_image)
    assert r[0] == 'failed' and 'FORBIDDEN under the LKMM' in r[1] and '0:r0=0; 1:r0=0 x1' in r[1]
    mp = [(19999, ':', '1:r0=x; 1:r1=1;'), (1, '*', '1:r0=x; 1:r1=0;')]
    r = v.public('lkmm-MP+onceassign+derefonce', {'rc': '0'}, lkmm('MP+onceassign+derefonce', mp), image=lkmm_image)
    assert r[0] == 'failed' and '1:r0=x; 1:r1=0 x1' in r[1]
    r = v.public('lkmm-CoWW+poonceonce', {'rc': '0'}, lkmm('CoWW+poonceonce', [(39999, ':', 'x=2;'), (1, '*', 'x=1;')]),
                 image=lkmm_image)
    assert r[0] == 'failed' and 'x=1 x1' in r[1]


def test_public_lkmm_incomplete_output_fails(lkmm_image):
    """A lost or garbled line could hide a forbidden state, a cut-short run (a thread not created) takes fewer samples:
    the state lines must number the histogram's, sum to Positive + Negative and to instances x size x runs."""
    v = verdicts()
    name, test = 'lkmm-SB+fencembonceonces', 'SB+fencembonceonces'
    good = [(10000, ':', '0:r0=0; 1:r0=1;'), (10000, ':', '0:r0=1; 1:r0=0;')]
    judge = lambda lines: v.public(name, {'rc': '0'}, lines, image=lkmm_image)
    assert judge(lkmm(test, good))[0] == 'passed'
    lines = lkmm(test, good); lines.remove('10000   :>0:r0=1; 1:r0=0;')
    r = judge(lines)
    assert r[0] == 'failed' and '1 state lines, the histogram declares 2' in r[1] and 'sum to 10000' in r[1]
    lines = lkmm(test, good); lines[lines.index('10000   :>0:r0=1; 1:r0=0;')] = '10000   :>0:r0=1; 1:r'
    r = judge(lines)
    assert r[0] == 'failed' and 'not states' in r[1]
    r = judge(lkmm(test, [(5000, ':', '0:r0=0; 1:r0=1;'), (5000, ':', '0:r0=1; 1:r0=0;')]))   # one instance of two
    assert r[0] == 'failed' and '10000 samples, the module takes 20000 (2 instances x 1000 x 10)' in r[1]
    r = judge(lkmm(test, good, total=20001))
    assert r[0] == 'failed' and 'Positive + Negative = 20001' in r[1]
    lines = [ln for ln in lkmm(test, good) if not ln.startswith('Positive:')]
    assert 'no "Positive: P, Negative: Q" line' in judge(lines)[1]
    lines = [ln for ln in lkmm(test, good) if not ln.startswith(('Test ', 'Histogram', '10000'))]
    assert judge(lines) == ('failed', '0 "Test SB+fencembonceonces" blocks in the output, not 1')
    lines = lkmm(test, good); i = lines.index('PUB-RESULT id=lkmm-SB+fencembonceonces rc=0')
    assert '2 "Test' in judge(lines[:i] + lkmm(test, good)[2:-3] + lines[i:])[1]            # the output twice
    assert judge(pub(name, 0, part='115/1'))[0] == 'failed'                                   # no output at all
    assert v.public(name, {'rc': '0'}, lkmm(test, good), image=None)[0] == 'failed'          # no oracle
    assert v.public('lkmm-LB+poonceonces', {'rc': '0'}, lkmm('LB+poonceonces', good), image=lkmm_image)[0] == 'failed'
    assert v.public(name, {'rc': '124'}, pub(name, 124, part='115/1'), image=lkmm_image)[0] == 'failed'   # guest cap


def test_public_lkmm_skipped_is_unsupported_only_when_threads_exceed_cpus(lkmm_image):
    """At 1 vCPU klitmus7 runs no instance of a two-thread test (`NAME: skipped`): unsupported, never passed; a skip
    where the threads fit (the one-thread CoWW at 1 vCPU, any test at 4) fails."""
    v = verdicts()
    skipped = lambda test, cpus: [ln.replace('cpus=4', f'cpus={cpus}') for ln in
                                  pub('lkmm-' + test, 0, part='115/1', body=[f'{test}: skipped'])]
    r = v.public('lkmm-SB+poonceonces', {'rc': '0'}, skipped('SB+poonceonces', 1), image=lkmm_image)
    assert r[0] == 'unsupported' and '2 threads, the guest has 1 CPUs' in r[1]
    assert v.public('lkmm-SB+poonceonces', {'rc': '0'}, skipped('SB+poonceonces', 4), image=lkmm_image)[0] == 'failed'
    assert v.public('lkmm-CoWW+poonceonce', {'rc': '0'}, skipped('CoWW+poonceonce', 1), image=lkmm_image)[0] == 'failed'
    empty = lkmm('SB+poonceonces', [], cpus=1)          # a block with no samples where no instance fits: never a pass
    r = v.public('lkmm-SB+poonceonces', {'rc': '0'}, empty, image=lkmm_image)
    assert r[0] == 'failed' and 'yet no `SB+poonceonces: skipped` line' in r[1]
    crlf = [ln + '\r' for ln in skipped('SB+poonceonces', 1)]       # the serial console's line ends
    assert v.public('lkmm-SB+poonceonces', {'rc': '0'}, crlf, image=lkmm_image)[0] == 'unsupported'
    assert 'image' in campaign.inspect.signature(v.public).parameters


# --- the committed family ---

# The libm32 programs whose recorded failure is `fp=*` (judged by failing alone), by mode, each with the reason its output
# differs between two recordings: none.
FP_STAR: dict[str, set[str]] = {}


def test_correctness_family_declares_its_campaigns():
    family.load.cache_clear()
    fam = family.load(REPO / 'tests' / 'correctness' / 'family.toml')
    assert {s.name for s in fam.suites} >= {'openssl', 'torture', 'libm', 'gstr', 'x86st', 'litmus'}
    for name in ('torture', 'libm', 'gstr', 'x86st'):   # every known non-pass names the document that records it
        assert all(re.search(r'docs/\S+\.md', why) for why in fam.suite(name).extra['expected'].values()), name
    assert fam.suite('x86st').extra['parts'] == 49        # 25 64-bit programs and, since M2 checkpoint A, 24 32-bit ones
    assert sorted(fam.suite('x86st').extra['expected']) == ['ldt_gdt_32', 'sigreturn_32', 'sigreturn_64', 'sysret_ss_attrs_64',
                                                            'test_shadow_stack_64', 'test_syscall_vdso_32']
    assert list(fam.suite('gstr').extra['expected']) == ['wcsmbs/tst-wcstod-round']
    libm = fam.suite('libm').extra['expected']      # correctness-userspace.md, finding 1: 320 by family + 12 named
    fams = re.compile(r'math/test-(ldouble|float64x|(float|double|float32|float32x|float64)-(ldouble|float64x))-')
    assert len(libm) == 332 and sum(map(bool, map(fams.match, libm))) == 320
    assert all('reduced precision' in why and 'correctness-userspace.md' in why for why in libm.values())
    v = verdicts()
    for s in fam.suites:
        # i386 has no tier: it is in the gate
        assert (s.kind, s.harness, s.extra.get('tier')) == ('judged', 'campaign', None if s.name == 'i386' else 'campaign')
        # every known non-pass names the document that records it, and no test is both always and only-without expected
        without = {t: why for d in s.extra.get('expected_without', {}).values() for t, why in d.items()}
        for t, why in {**s.extra.get('expected', {}), **without, **s.extra.get('flaky', {})}.items():
            assert re.search(r'docs/\S+\.md', why), (s.name, t)
        assert not set(s.extra.get('expected', {})) & set(without), s.name
        mod, fn = s.extra['verdict'].split(':')
        assert mod == 'verdicts' and callable(getattr(v, fn))
        if s.name in ('openssl', 'torture'): assert s.matrix == {'machine.vcpus': [4]}
    # the 32-bit campaigns: answers from the x86 reference machine, LTP's from the same kernel under KVM; their answer
    # files exist (part-0 reports a stale one)
    for name in ('i386', 'torture32', 'ltp32', 'libm32'): assert fam.suite(name).reference == 'x86', name
    assert fam.suite('ltp32').extra.get('reference_boot') == 'kvm' and 'reference_boot' not in fam.suite('libm32').extra
    for name in ('ltp32', 'libm32'):
        assert all((fam.dir / a).is_file() for a in fam.suite(name).extra['answers']), name
    assert fam.suite('libm32').matrix == {'machine.vcpus': [4], 'host.afp': [True, False], 'engine.fp': ['fast', 'accurate']}
    # each AFP-only entry names its deviation (tests/lib/fp_deviations.py), which decides whether accurate mode keeps it
    paths.use_test_scripts()
    import fp_deviations
    names = {*fp_deviations.CLASSES, *fp_deviations.X87_OTHER}
    for name in ('libm', 'libm32'):
        for t, why in fam.suite(name).extra['expected_without']['afp'].items():
            assert why.split(':', 1)[0] in names, (name, t)
    kept = [t for t, why in fam.suite('libm32').extra['expected_without']['afp'].items() if campaign.kept_in_accurate(why)]
    assert len(kept) == 15 and all(not t.endswith('fma') or '-float-double-' in t or '-float32-float' in t for t in kept)
    # libm32's recorded failures: in each mode exactly the programs expected to fail there, and none judged by failing
    # alone (`fp=*`, a failure that differed between two recordings) but those listed here, each with its reason
    libm32 = fam.suite('libm32').extra
    always, without = set(libm32['expected']), set(libm32['expected_without']['afp'])
    for mode, want in (('afp', always), ('noafp', always | without), ('noafp-accurate', always | set(kept))):
        recorded = v._answers(fam.dir / f'libm32-fails-{mode}.txt')
        assert set(recorded) == want, (mode, sorted(set(recorded) ^ want)[:5])
        assert {t for t, fp in recorded.items() if fp == 'fp=*'} == FP_STAR.get(mode, set()), mode
    t = fam.suite('torture').extra['expected']
    assert sorted(t) == sorted(f'{n}.{o}' for n in ('ieee_inf-2', 'ieee_inf-3', 'ieee_pr36332', 'pr39228') for o in ('O0', 'O2'))
    assert all('reduced precision' in why and 'docs/restructure/correctness-userspace.md' in why for why in t.values())


def test_correctness_family_declares_litmus():
    """litmus: two boots at 4 vCPUs (the original's), judged by herd7's states through the image folder."""
    family.load.cache_clear()
    s = family.load(REPO / 'tests' / 'correctness' / 'family.toml').suite('litmus')
    assert (s.kind, s.harness, s.extra['tier'], s.extra['verdict']) == ('judged', 'campaign', 'campaign', 'verdicts:litmus')
    assert s.extra['parts'] == 2 and s.matrix == {'machine.vcpus': [4]} and s.smoke == ['part-0']
    assert s.extra['part_args'] == 'r=10,s=10000'           # run-litmus.sh's -r/-s: the smoke size, not the compiled one
    assert 'image' in campaign.inspect.signature(verdicts().litmus).parameters
    assert not s.extra.get('expected') and not s.extra.get('flaky')


# --- how the outcomes are reported ---

def test_sub_result_categories_reach_the_report():
    import types
    from testkit import collect, report
    from testkit.harness import SubResult
    got = []
    item = types.SimpleNamespace(nodeid='tests/correctness/family.toml::openssl/part-0[vcpus=4]', path=REPO / 'x',
                                 ihook=types.SimpleNamespace(pytest_runtest_logreport=lambda report: got.append(report)))
    for s in (SubResult('a', 'failed', 'listed but not in the output', category='missing'),
              SubResult('b', 'skipped', 'x87 (docs/x.md)', category='expected-fail'),
              SubResult('c', 'passed')):
        collect.emit_sub(item, s)
    a, b, c = got
    assert a.failed and report.category(a) == 'missing' and report.teststatus(a)[0] == 'missing'
    assert dict(a.user_properties)['testkit-note'] == 'listed but not in the output'
    assert b.skipped and report.teststatus(b)[0] == 'expected-fail'
    assert c.passed and report.category(c) is None


def test_campaign_categories_and_gate():
    from testkit import report
    for c in ('flaky', 'expected-fail', 'unverified', 'missing', 'unexpected-pass', 'stale-entry'):
        assert c in report.CATEGORIES
    for c in ('missing', 'unexpected-pass', 'stale-entry'):
        assert c in report.GATE_FAILS
    for c in ('flaky', 'expected-fail', 'unverified'):
        assert c not in report.GATE_FAILS
    assert report.gate_verdict({'missing': [1]}) == (False, ['1 missing'])
    assert report.gate_verdict({'flaky': [1], 'expected-fail': [1, 2]})[0]


def test_a_bad_list_fails_only_its_part(tmp_path, monkeypatch):
    import types
    fam, suite = make_family(tmp_path, parts=1)
    img = tmp_path / 'image'; img.mkdir()
    (img / 'openssl-cases.txt').write_text('just-a-name\n')
    monkeypatch.setattr(campaign, 'image', lambda name: img)
    from testkit import runloop
    monkeypatch.setattr(runloop, 'run_dir', lambda config: tmp_path / 'run')
    it = types.SimpleNamespace(nodeid='n0', fam=fam, suite=suite, case='part-0', variant=family.expand(suite)[0])
    r = campaign.HARNESS.run([it], None)['n0']
    assert r.outcome == 'failed' and 'not "PART TEST"' in r.message



def test_ct_end_only_at_the_start_of_a_line():
    """Review: a CT-END inside another line (a test's output, a CT-LOG line) was taken as the part's end."""
    from testkit.harness import campaign
    assert campaign.end_of('CT-LOG t | echo CT-END suite=x part=0/1\n') is None
    assert campaign.end_of('\rCT-END suite=x part=0/1 rc=0\n') == {'suite': 'x', 'part': '0/1', 'rc': '0'}


def test_a_part_failing_on_its_own_is_not_masked_by_a_known_bug(tmp_path):
    """Review: known_bugs reclassified a failed case whose failed sub-results were all known bugs, even when the part
    itself failed (no CT-END): such a CaseResult is `own` and stays failed."""
    from _pytest.reports import TestReport
    from testkit import known_bugs
    (tmp_path / 'tests' / 'f').mkdir(parents=True)
    (tmp_path / 'tests' / 'f' / 'known-bugs.toml').write_text('[bug.b]\nnote = "n"\ncases = ["s/part-0::t1"]\n')
    known_bugs._bugs.cache_clear()
    t = known_bugs.Tracker(tmp_path)
    mk = lambda nid, out, props=(): TestReport(nid, ('tests/f/family.toml', 0, nid), {}, out, None if out == 'passed' else 'x',
                                               'call', user_properties=list(props))
    t.adjust(mk('tests/f/family.toml::s/part-0::t1', 'failed'))
    r = t.adjust(mk('tests/f/family.toml::s/part-0', 'failed', [('testkit-own-failure', '1')]))
    assert r.failed


def test_a_terminated_worker_stops_its_natemu(tmp_path):
    """Review: natemu runs in its own session, so the run loop's SIGTERM/SIGKILL to the worker's group missed it and a
    VM outlived its unit (and its cap). A worker that gets SIGTERM now stops every natemu it started."""
    import subprocess, sys, textwrap, time
    script = tmp_path / 'worker.py'
    script.write_text(textwrap.dedent(f'''
        import subprocess, sys, time
        sys.path.insert(0, {str(paths_root())!r})
        from testkit.harness import campaign
        p = subprocess.Popen(['sleep', '60'], start_new_session=True)
        campaign._LIVE.add(p); campaign._on_term()
        print(p.pid, flush=True)
        time.sleep(60)
    '''))
    w = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    child = int(w.stdout.readline())
    w.terminate(); w.wait(20)
    deadline = time.time() + 20                     # killed and reaped (by launchd: its parent is gone) within a while
    while subprocess.run(['kill', '-0', str(child)], capture_output=True).returncode == 0:
        assert time.time() < deadline, 'the child outlived its worker'; time.sleep(0.05)


def paths_root():
    from testkit import paths
    return paths.repo_root()


# --- CPython (cpyt, cpyb): the ct= word and modules without a result line ---

CPY_LIST = '''\
    0/2 test_a
    0/2 test_b
    0/2 test_winreg
    0/2 test_curses
    0/2 test_gone
    1/2 test_c
    '''

# run-cpy.sh's output: regrtest's (CT-LOG |) with its progress lines and closing summary, then the JUnit results.
CPY_LOG = ['CT-LOG rc=2',
           'CT-LOG | 0:00:00 load avg: 0.00 Run 5 tests in parallel using 4 worker processes',
           "CT-LOG | 0:00:01 load avg: 0.00 [1/5] test_winreg skipped -- No module named 'winreg'",
           'CT-LOG | 0:00:01 load avg: 0.00 [2/5] test_curses skipped (resource denied) -- Use of the `curses` resource not enabled',
           'CT-LOG | 0:00:02 load avg: 0.00 [3/5] test_a passed',
           'CT-LOG | 0:00:03 load avg: 0.00 [4/5/1] test_b failed (1 failure)',
           'CT-LOG | ',
           'CT-LOG | == Tests result: FAILURE ==',
           'CT-LOG | ',
           'CT-LOG | 1 test skipped:',
           'CT-LOG |     test_winreg',
           'CT-LOG | ',
           'CT-LOG | 1 test skipped (resource denied):',
           'CT-LOG |     test_curses',
           'CT-LOG | ',
           'CT-LOG | 1 test failed:',
           'CT-LOG |     test_b',
           'CT-LOG | ',
           'CT-LOG | 1 test OK.',
           'CT-CASE test.test_b.T.test_x fail',
           'CT-RESULT cpy test_a cases=4 pass=4 fail=0 error=0 skip=0 sec=1.0',
           'CT-RESULT cpy test_b cases=2 pass=1 fail=1 error=0 skip=0 sec=1.0']
CPYT = '''\
    ct = "cpy"
    unreported = "verdicts:cpyt_unreported"
    '''


def test_ct_key_is_the_boot_word_and_ct_end_suite(setup):
    run, fake, _ = setup(CPY_LIST, {'1/2': serial('1/2', 'CT-RESULT cpy test_c cases=1 pass=1 fail=0 error=0 skip=0 sec=1',
                                                  suite='cpy')},
                         suite='cpyt', verdict='cpyt', extra=CPYT)
    res = run('part-1')
    assert res.outcome == 'passed', res.message
    assert fake.calls[0]['suite'] == 'cpy' and fake.calls[0]['image'].name == 'cpyt.cpio.zst'


def test_ct_end_of_another_suite_word_fails(setup):
    run, _, _ = setup(CPY_LIST, {'1/2': serial('1/2', 'CT-RESULT cpy test_c cases=1 pass=1 fail=0 error=0 skip=0 sec=1',
                                               suite='cpyt')},
                      suite='cpyt', verdict='cpyt', extra=CPYT)
    res = run('part-1')
    assert res.outcome == 'failed' and 'not suite=cpy part=1/2' in res.message


def test_ct_defaults_to_the_suite_name(setup):
    run, fake, _ = setup(OSSL_LIST, {'0/2': serial('0/2', 'CT-RESULT openssl default:test_a rc=0 ok=3 notok=0',
                                                   'CT-RESULT openssl generic:test_a rc=0 ok=3 notok=0')})
    assert run('part-0').outcome == 'passed' and fake.calls[0]['suite'] == 'openssl'


def test_cpy_module_without_a_result_line_takes_regrtests_verdict(setup):
    """A module regrtest skipped whole reports no JUnit case, so no CT-RESULT line: its verdict comes from regrtest's
    own output (cpy-check.py's reading), and a module regrtest never named stays missing."""
    run, _, _ = setup(CPY_LIST, {'0/2': serial('0/2', *CPY_LOG, suite='cpy')}, suite='cpyt', verdict='cpyt', extra=CPYT)
    res = run('part-0')
    got = subs(res)
    assert got == {'test_a': ('passed', None), 'test_b': ('failed', None), 'test_winreg': ('skipped', 'unsupported'),
                   'test_curses': ('skipped', 'unsupported'), 'test_gone': ('failed', 'missing')}, res.message
    msg = {s.name: s.longrepr for s in res.sub}
    assert 'regrtest: skipped' in msg['test_winreg'] and "No module named 'winreg'" in msg['test_winreg']
    assert 'regrtest: skipped (resource denied)' in msg['test_curses']
    assert 'test.test_b.T.test_x fail' in msg['test_b']


def test_without_the_unreported_key_such_modules_are_missing(setup):
    run, _, _ = setup(CPY_LIST, {'0/2': serial('0/2', *CPY_LOG, suite='cpy')}, suite='cpyt', verdict='cpyt',
                      extra='ct = "cpy"\n')
    got = subs(run('part-0'))
    assert got['test_winreg'] == ('failed', 'missing') and got['test_a'] == ('passed', None)


def test_cpyt_unreported_verdicts():
    v = verdicts()
    text = '\n'.join(CPY_LOG + ['CT-LOG | 0:00:04 load avg: 0.00 [5/5/2] test_d failed (env changed)', 'CT-LOG | ',
                                'CT-LOG | 1 test altered the execution environment (env changed):', 'CT-LOG |     test_d',
                                'CT-LOG | ', 'CT-LOG | 1 test run no tests:', 'CT-LOG |     test_e']) + '\n'
    assert v.cpyt_unreported('test_winreg', text)[0] == 'unsupported'
    assert v.cpyt_unreported('test_curses', text)[0] == 'unsupported'
    assert v.cpyt_unreported('test_e', text)[0] == 'unsupported'
    assert v.cpyt_unreported('test_d', text)[0] == 'failed'
    assert v.cpyt_unreported('test_b', text)[0] == 'failed'
    assert v.cpyt_unreported('test_a', text)[0] == 'passed'          # listed as run, no case recorded
    assert v.cpyt_unreported('test_gone', text) is None
    crlf = text.replace('\n', '\r\n')                                   # the serial console's line ends
    assert v.cpyt_unreported('test_winreg', crlf)[0] == 'unsupported'


def test_an_unreported_verdict_that_raises_fails_the_test(setup, monkeypatch):
    run, _, _ = setup(CPY_LIST, {'0/2': serial('0/2', *CPY_LOG, suite='cpy')}, suite='cpyt', verdict='cpyt', extra=CPYT)
    real = campaign._verdict

    def boom(name, serial):
        raise ValueError('unreadable')
    monkeypatch.setattr(campaign, '_verdict', lambda d, n, spec: boom if spec.endswith('unreported') else real(d, n, spec))
    got = subs(run('part-0'))
    assert got['test_winreg'] == ('failed', None) and got['test_a'] == ('passed', None)


def test_cpyb_judges_its_smoke_modules_as_cpyt():
    v = verdicts()
    assert v.cpyb('test_os', {'kind': 'cpy', 'cases': '5', 'pass': '4', 'fail': '1', 'error': '0', 'skip': '0'}, [])[0] == 'failed'
    assert v.cpyb('test_os', {'kind': 'cpy', 'cases': '5', 'pass': '5', 'fail': '0', 'error': '0', 'skip': '0'}, [])[0] == 'passed'
    assert v.cpyb('make', {'kind': 'cpyb', 'rc': '0', 'sec': '1'}, [])[0] == 'passed'


def test_correctness_family_declares_cpython():
    family.load.cache_clear()
    fam = family.load(REPO / 'tests' / 'correctness' / 'family.toml')
    t, b = fam.suite('cpyt'), fam.suite('cpyb')
    assert (t.extra['ct'], t.extra['parts'], t.extra['verdict']) == ('cpy', 12, 'verdicts:cpyt')
    assert b.extra['parts'] == 1 and b.extra.get('ct', 'cpyb') == 'cpyb'
    v = verdicts()
    for s in (t, b):
        assert callable(getattr(v, s.extra['unreported'].split(':')[1]))
    # flaky at case level only (verdicts.CPY_FLAKY_CASES), never a whole module
    assert 'flaky' not in t.extra and 'flaky' not in b.extra and t.extra['verdict_flaky'] is b.extra['verdict_flaky'] is True
    assert sorted(c.rsplit('.', 1)[1] for c in v.CPY_FLAKY_CASES) == [
        'test_timerfd_TFD_TIMER_ABSTIME', 'test_timerfd_initval', 'test_timerfd_ns_TFD_TIMER_ABSTIME']
    assert all('docs/restructure/correctness-cpython-string.md' in why for why in v.CPY_FLAKY_CASES.values())
    assert (t.extra['unreported'], b.extra['unreported']) == ('verdicts:cpyt_unreported', 'verdicts:cpyb_unreported')
    # the /dev/stdin and ZIP-mtime failures are fixed in the image (recipes/common.sh): never expected
    assert not {'test_asyncio.test_subprocess', 'test_zipfile', 'test_cmd_line_script'} & set(t.extra.get('expected', {}))


# --- review fixes: a regrtest that died, cpyb's smoke modules, flaky cases not modules ---

DIED = ['CT-LOG rc=137',
        'CT-LOG | 0:00:01 load avg: 0.00 [1/4] test_a passed',
        'CT-LOG | 0:00:02 load avg: 0.00 [2/4/1] test_b failed (1 failure)',
        'CT-LOG | 0:00:03 load avg: 0.00 [3/4/2] test_c crashed (Exit code -11)',
        "CT-LOG | 0:00:03 load avg: 0.00 [4/4/2] test_d skipped -- No module named '_ssl'"]


def test_unreported_without_regrtests_summary_trusts_only_progress_lines():
    """regrtest died before its closing summary: a module passes only on its own `passed` line."""
    v = verdicts()
    text = '\n'.join(DIED) + '\n'
    assert v.cpyt_unreported('test_a', text)[0] == 'passed'
    b, c = v.cpyt_unreported('test_b', text), v.cpyt_unreported('test_c', text)
    assert b[0] == 'failed' and 'did not finish' in b[1]
    assert c[0] == 'failed' and 'crashed (Exit code -11)' in c[1]
    assert v.cpyt_unreported('test_d', text)[0] == 'unsupported'
    assert v.cpyt_unreported('test_e', text) is None


def test_a_crashed_progress_line_fails_whatever_the_summary_says():
    v = verdicts()
    text = '\n'.join(DIED + ['CT-LOG | ', 'CT-LOG | == Tests result: FAILURE ==', 'CT-LOG | ', 'CT-LOG | 2 tests OK.']) + '\n'
    assert v.cpyt_unreported('test_c', text)[0] == 'failed'
    assert v.cpyt_unreported('test_a', text)[0] == 'passed'


def test_cpyb_smoke_module_not_exercised_fails():
    v = verdicts()
    text = '\n'.join(CPY_LOG) + '\n'
    assert v.cpyb_unreported('test_winreg', text)[0] == 'failed'          # skipped whole: the build lacks it
    assert v.cpyb_unreported('test_curses', text)[0] == 'failed'
    assert v.cpyb_unreported('test_a', text)[0] == 'passed' and v.cpyb_unreported('test_gone', text) is None
    allskip = {'kind': 'cpy', 'cases': '3', 'pass': '0', 'fail': '0', 'error': '0', 'skip': '3'}
    assert v.cpyb('test_ssl', allskip, [])[0] == 'failed' and v.cpyt('test_ssl', allskip, [])[0] == 'unsupported'
    make = {'kind': 'cpyb', 'rc': '0', 'sec': '100'}
    assert v.cpyb('make', make, ['CT-LOG make | ./x.c:1: warning: unused'])[0] == 'passed'
    r = v.cpyb('make', make, ['CT-LOG make | Could not build the ssl module!'])
    assert r[0] == 'failed' and 'Could not build the ssl module' in r[1]
    assert v.cpyb('make', make, ['CT-LOG make | Failed to build these modules:'])[0] == 'failed'
    # make's own summary names the counts: a module missing its dependency is not a failed build (run 20261119-012841-3c3e)
    summary = 'CT-LOG make | Checked 112 modules (33 built-in, 77 shared, 1 n/a on linux-x86_64, 0 disabled, 1 missing, {} failed on import)'
    assert v.cpyb('make', make, [summary.format(0)])[0] == 'passed'
    assert v.cpyb('make', make, [summary.format(2)])[0] == 'failed'


TFD = 'CT-CASE test.test_os.TimerfdTests.test_timerfd_TFD_TIMER_ABSTIME fail'


def test_only_the_timerfd_cases_are_flaky():
    v = verdicts()
    f = lambda fail, error='0': {'cases': '300', 'pass': '299', 'fail': fail, 'error': error, 'skip': '0'}
    r = v.cpyt('test_os', f('1'), [TFD])
    assert r[0] == 'flaky' and 'docs/restructure/correctness-cpython-string.md' in r[1]
    assert v.cpyt('test_os', f('2'), [TFD, 'CT-CASE test.test_os.FileTests.test_write fail'])[0] == 'failed'
    assert v.cpyt('test_os', f('2'), [TFD])[0] == 'failed'               # a failure no CT-CASE line names
    assert v.cpyt('test_os', f('1'), ['CT-CASE test.test_os.FileTests.test_write fail'])[0] == 'failed'
    assert v.cpyt('test_time', f('1'), ['CT-CASE test.test_time.T.test_timerfd_initval fail'])[0] == 'failed'
    # an error is the timerfd API itself failing (EINVAL from timerfd_settime, say), never a timing budget
    errs = [f'CT-CASE {c} error' for c in v.CPY_FLAKY_CASES]
    assert v.cpyt('test_os', f('0', '3'), errs)[0] == 'failed'
    assert v.cpyt('test_os', f('1', '1'), [TFD, errs[1]])[0] == 'failed'


def test_make_fails_on_a_failed_extension_line_in_any_case():
    v = verdicts()
    make = {'kind': 'cpyb', 'rc': '0', 'sec': '100'}
    for line in ('Failed to build these modules:', 'Following modules built successfully but were removed because '
                 'they could not be imported:', '*** WARNING: renaming "_ssl" since importing it failed on import'):
        assert v.cpyb('make', make, [f'CT-LOG make | {line}'])[0] == 'failed', line


def test_run_cpyb_prints_every_failed_extension_line_after_the_warnings(tmp_path):
    """run-cpyb.sh's make-log lines: warnings capped at 40, failed extensions all, whatever their case."""
    import subprocess
    src = (REPO / 'tools/correctness/run-cpyb.sh').read_text()
    cmds = [l for l in src.splitlines() if 'make.log' in l and l.startswith('grep')]
    assert len(cmds) == 2
    log = tmp_path / 'make.log'
    log.write_text(''.join(f'x.c:{i}: warning: w\n' for i in range(60)) +
                   'Failed to build these modules:\n_ssl\nthey could not be imported: _x\n')
    out = subprocess.run(['sh', '-c', '\n'.join(cmds)], env={'L': str(tmp_path), 'PATH': '/usr/bin:/bin'},
                         capture_output=True, text=True).stdout.splitlines()
    assert sum(' warning: ' in l for l in out) == 40
    assert 'CT-LOG make | Failed to build these modules:' in out and 'CT-LOG make | they could not be imported: _x' in out


OS_LIST = '''\
    0/1 test_os
    0/1 test_a
    '''


def _os(fail, *cases):
    return serial('0/1', *cases, f'CT-RESULT cpy test_os cases=300 pass={300 - fail} fail={fail} error=0 skip=0 sec=1',
                  'CT-RESULT cpy test_a cases=1 pass=1 fail=0 error=0 skip=0 sec=1', suite='cpy')


VFLAKY = '''\
    ct = "cpy"
    verdict_flaky = true
    '''


def test_flaky_verdict_reruns_once_and_a_pass_counts(setup):
    run, fake, _ = setup(OS_LIST, {'0/1': [_os(1, TFD), _os(0)]}, suite='cpyt', verdict='cpyt', parts=1, extra=VFLAKY)
    res = run('part-0')
    assert len(fake.calls) == 2 and res.outcome == 'passed', res.message
    assert subs(res)['test_os'] == ('passed', None)


def test_flaky_verdict_twice_is_flaky(setup):
    run, fake, _ = setup(OS_LIST, {'0/1': [_os(1, TFD), _os(1, TFD)]}, suite='cpyt', verdict='cpyt', parts=1, extra=VFLAKY)
    res = run('part-0')
    assert len(fake.calls) == 2 and res.outcome == 'passed' and subs(res)['test_os'] == ('skipped', 'flaky'), res.message


def test_flaky_verdict_then_a_real_failure_fails(setup):
    other = 'CT-CASE test.test_os.FileTests.test_write fail'
    run, fake, _ = setup(OS_LIST, {'0/1': [_os(1, TFD), _os(1, other)]}, suite='cpyt', verdict='cpyt', parts=1, extra=VFLAKY)
    res = run('part-0')
    assert res.outcome == 'failed' and subs(res)['test_os'] == ('failed', None)


def test_a_module_failing_beyond_the_timerfd_cases_is_not_rerun(setup):
    other = 'CT-CASE test.test_os.FileTests.test_write fail'
    run, fake, _ = setup(OS_LIST, {'0/1': _os(2, TFD, other)}, suite='cpyt', verdict='cpyt', parts=1, extra=VFLAKY)
    res = run('part-0')
    assert len(fake.calls) == 1 and subs(res)['test_os'] == ('failed', None)


def test_verdict_flaky_leaves_room_for_the_rerun(tmp_path):
    fam, suite = make_family(tmp_path / 'a', suite='cpyt', verdict='cpyt', extra=VFLAKY)
    plain_fam, plain = make_family(tmp_path / 'b', suite='cpyt', verdict='cpyt', extra='ct = "cpy"\n')
    one = 60 + campaign.AFTER_END + 30
    assert campaign.HARNESS.case_timeout(plain_fam, plain, 'part-0') == one + 120
    assert campaign.HARNESS.case_timeout(fam, suite, 'part-0') == 2 * one + 120


def test_a_flaky_verdict_without_verdict_flaky_fails_and_is_not_rerun(setup):
    run, fake, _ = setup(OS_LIST, {'0/1': [_os(1, TFD), _os(0)]}, suite='cpyt', verdict='cpyt', parts=1,
                         extra='ct = "cpy"\n')
    res = run('part-0')
    assert len(fake.calls) == 1 and subs(res)['test_os'] == ('failed', None) and 'no verdict_flaky' in res.sub[0].longrepr


def test_every_boot_gets_the_ct_word_part_options_and_boot_file(setup, monkeypatch):
    """The first boot and the verdict-flaky rerun alike: ct= word, the part word with part_args, the part's
    <suite>-boot.txt kernel and words."""
    run, fake, img = setup(OS_LIST, {'0/1,scale=2': [_os(1, TFD), _os(0)]}, suite='cpyt', verdict='cpyt', parts=1,
                           extra=VFLAKY + 'part_args = "scale=2"\n')
    (img / 'k').write_bytes(b'bzImage')
    (img / 'cpyt-boot.txt').write_text('0/1 kernel=k x=1\n')
    monkeypatch.delenv(campaign.PART_ARGS_ENV, raising=False)
    got = []

    def fake_kw(image, suite, part, vcpus, work, cap, **kw):
        got.append((suite, part, kw))
        return fake(image, suite, part, vcpus, work, cap)
    monkeypatch.setattr(campaign, 'boot', fake_kw)
    res = run('part-0')
    assert res.outcome == 'passed', res.message
    assert got == [('cpy', '0/1,scale=2', {'kernel': img / 'k', 'args': ('x=1',)})] * 2


# --- per-part kernels and command-line words (<suite>-boot.txt: the public campaign) ---

def test_boot_file_gives_each_part_its_kernel_and_words(setup, monkeypatch):
    """public: a part names its kernel (a file of the image folder) and its guest cap (pubcap=), which reach boot()."""
    run, fake, img = setup('0/2 default:test_a\n1/2 default:test_b\n',
                           {'0/2': serial('0/2', 'CT-RESULT openssl default:test_a rc=0 ok=1 notok=0'),
                            '1/2': serial('1/2', 'CT-RESULT openssl default:test_b rc=0 ok=1 notok=0')})
    (img / 'test-kernel').write_bytes(b'bzImage')
    (img / 'openssl-boot.txt').write_text('0/2 pubcap=300\n1/2 kernel=test-kernel pubcap=90 x=1\n')
    got = []

    def fake_kw(image, suite, part, vcpus, work, cap, **kw):
        got.append((part, kw))
        return fake(image, suite, part, vcpus, work, cap)
    monkeypatch.setattr(campaign, 'boot', fake_kw)
    assert run('part-0').outcome == 'passed' and run('part-1').outcome == 'passed'
    assert got == [('0/2', {'args': ('pubcap=300',)}), ('1/2', {'args': ('pubcap=90', 'x=1'), 'kernel': img / 'test-kernel'})]


def test_boot_file_naming_a_missing_kernel_fails_the_part(setup):
    run, fake, img = setup('0/1 default:test_a\n', {'0/1': serial('0/1', 'CT-RESULT openssl default:test_a rc=0 ok=1 notok=0')},
                           parts=1)
    (img / 'openssl-boot.txt').write_text('0/1 kernel=../elsewhere\n')
    with pytest.raises(ValueError, match='no kernel'):
        run('part-0')
    assert not fake.calls


def test_boot_uses_the_parts_kernel_and_words(tmp_path, monkeypatch):
    """boot() with a part's kernel and words: --kernel is that file, the words end the command line."""
    import types
    stub = tmp_path / 'natemu'
    stub.write_text('#!/bin/sh\nall="$0 $*"\nwhile [ $# -gt 0 ]; do case $1 in --serial) s=$2;; esac; shift; done\n'
                    'printf "CT-END suite=public part=3/1 rc=0 guest_s=1\\n" > "$s"\necho "$all" > "$(dirname "$s")/argv"\n')
    stub.chmod(0o755)
    monkeypatch.setattr(campaign.artifacts, 'natemu', lambda: types.SimpleNamespace(stub=stub, runtime=tmp_path / 'rt'))
    monkeypatch.setattr(campaign.artifacts, 'fexc', lambda: tmp_path / 'fexc')
    monkeypatch.setattr(campaign.resources, 'fetch', lambda name: tmp_path / name)
    work = tmp_path / 'w'
    campaign.boot(tmp_path / 'public.cpio.zst', 'public', '3/1', 1, work, 30, kernel=tmp_path / 'public-kernel',
                  args=('pubcap=300',))
    argv = (work / 'argv').read_text().split()
    assert argv[argv.index('--kernel') + 1] == str(tmp_path / 'public-kernel')
    assert argv[argv.index('--cmdline') + 1:argv.index('--vcpus')] == ['console=ttyS0', 'mitigations=off', 'nokaslr',
                                                                       'ct=public', 'part=3/1', 'pubcap=300']


def test_public_rows_list_boot_and_suite_agree(tmp_path):
    """recipes/public/cases.py: the guest's table (line K+1 for part K/1), the list, the boot file and the suite agree:
    every row has its part, its kernel and pubcap=; the expected entries name rows; parts = the row count."""
    import subprocess, sys
    subprocess.run([sys.executable, '-I', str(REPO / 'tools/correctness/recipes/public/cases.py'), str(tmp_path)],
                   check=True, capture_output=True)
    rows = [l.split('\t', 1) for l in (tmp_path / 'public-cases.tsv').read_text().splitlines()]
    listed = campaign.read_list(tmp_path, 'public')
    assert listed == [(f'{k}/1', [r[0]]) for k, r in enumerate(rows)]
    (tmp_path / 'public-kernel').write_bytes(b'bzImage')
    boot = campaign.read_boot(tmp_path, 'public')
    assert list(boot) == [p for p, _ in listed]
    assert all(o['args'] == ('pubcap=300',) for o in boot.values())
    on_kernel = {r[0] for k, r in enumerate(rows) if 'kernel' in boot[f'{k}/1']}
    assert on_kernel == {r[0] for r in rows if 'modprobe' in r[1] or 'insmod' in r[1] or r[0] == 'mm-cow-gup'}
    family.load.cache_clear()
    s = family.load(REPO / 'tests' / 'correctness' / 'family.toml').suite('public')
    assert int(s.extra['parts']) == len(rows) and s.matrix == {'machine.vcpus': [1, 4]}
    assert set(s.extra['expected']) <= {r[0] for r in rows}
    assert all('docs/restructure/correctness-public-kernel.md' in why for why in s.extra['expected'].values())


def test_public_lkmm_rows(tmp_path):
    """cases.py's LKMM rows: the original campaign's 35 tests (Linux 6.12.107's tools/memory-model/litmus-tests, sorted
    bytewise as klitmus7 numbers them: CoWW+poonceonce is litmus003), after the 88 others so their parts keep their
    numbers, each loading its own module on the test kernel; the smoke list has one."""
    import subprocess, sys
    subprocess.run([sys.executable, '-I', str(REPO / 'tools/correctness/recipes/public/cases.py'), str(tmp_path)],
                   check=True, capture_output=True)
    rows = [l.split('\t', 1) for l in (tmp_path / 'public-cases.tsv').read_text().splitlines()]
    names = [n for n in (REPO / 'tools/correctness/recipes/public/lkmm-tests.txt').read_text().splitlines()
             if n and not n.startswith('#')]
    assert len(names) == 35 and names == sorted(names) and names[3] == 'CoWW+poonceonce'
    assert len({n.lower() for n in names}) == 34     # the two Z6.0 lock tests differ only in case: both are listed
    lkmm = [(k, r) for k, r in enumerate(rows) if r[0].startswith('lkmm-')]
    assert [k for k, _ in lkmm] == list(range(88, 123))
    for i, (k, (test, command)) in enumerate(lkmm):
        assert test == 'lkmm-' + names[i]
        assert command == f'insmod /opt/public/lkmm/litmus{i:03d}.ko && cat /proc/litmus && rmmod litmus{i:03d}'
    (tmp_path / 'public-kernel').write_bytes(b'bzImage')
    boot = campaign.read_boot(tmp_path, 'public')
    assert all(boot[f'{k}/1'].get('kernel') == tmp_path / 'public-kernel' for k, _ in lkmm)
    family.load.cache_clear()
    s = family.load(REPO / 'tests' / 'correctness' / 'family.toml').suite('public')
    smoke = [rows[int(p.removeprefix('part-'))][0] for p in s.smoke]
    assert [t for t in smoke if t.startswith('lkmm-')] == ['lkmm-SB+poonceonces']
    assert smoke[:6] == ['rseq-basic_test', 'qemu-cmpxchg', 'ltp-mmap01', 'kunit-memcpy_kunit', 'kernel-test_bitmap',
                         'locktorture-spin_lock']
