"""The campaign harness (testkit/harness/campaign.py) and the campaigns' verdicts (tests/correctness/verdicts.py). `boot` is
replaced by a fake that returns canned serial text: nothing here starts a VM or builds an image."""
import pathlib
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
    (img / f'{suite}-list.txt').write_text(textwrap.dedent(listing))
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

    def __call__(self, image, suite, part, vcpus, work, cap):
        self.calls.append(dict(image=image, suite=suite, part=part, vcpus=vcpus, work=work, cap=cap))
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
    log = ['CT-LOG x86.SB | 499990:>0:EAX=1; 1:EAX=0;', 'CT-LOG x86.SB | 5     *>0:EAX=0; 1:EAX=0;',
           'CT-LOG x86.SB | 5     :>0:EAX=1; 1:EAX=1;', 'CT-LOG x86.SB | 0:>0:EAX=0; 1:EAX=1;']
    r = v.litmus('x86.SB', {'rc': '0', 'obs': 'Sometimes', 'sec': '3'}, log, image=litmus_image)
    assert r[0] == 'failed' and 'FORBIDDEN' in r[1] and '0:eax=0; 1:eax=0 x5' in r[1]


def test_litmus_unseen_allowed_state_passes_with_note(litmus_image):
    v = verdicts()
    log = ['CT-LOG x86.SB | 499990:>0:EAX=1; 1:EAX=0;', 'CT-LOG x86.SB | 10    :>0:EAX=1; 1:EAX=1;']
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
    v = verdicts()
    assert v.public('rseq-basic_test', {'rc': '0'}, pub('rseq-basic_test', 0))[0] == 'unverified'
    assert v.public('rseq-basic_test', {'rc': '4'}, pub('rseq-basic_test', 4))[0] == 'unsupported'
    assert v.public('rseq-basic_test', {'rc': '1'}, pub('rseq-basic_test', 1))[0] == 'failed'
    assert v.public('rseq-basic_test', {'rc': '124'}, pub('rseq-basic_test', 124))[0] == 'failed'     # guest cap
    assert v.public('rseq-basic_test', {'rc': '0'}, pub('rseq-other', 0))[0] == 'failed'             # identity
    assert v.public('ltp-mmap01', {'rc': '32'}, pub('ltp-mmap01', 32))[0] == 'unsupported'
    assert v.public('ltp-mmap01', {'rc': '2'}, pub('ltp-mmap01', 2))[0] == 'failed'                  # broken
    r = v.public('mm-cow', {'rc': '0'}, pub('mm-cow', 0, body=['not ok 3 cow on fork']))
    assert r[0] == 'failed' and 'failure' in r[1]


# --- the committed family ---

def test_correctness_family_declares_openssl_and_torture():
    family.load.cache_clear()
    fam = family.load(REPO / 'tests' / 'correctness' / 'family.toml')
    assert {s.name for s in fam.suites} == {'openssl', 'torture'}
    v = verdicts()
    for s in fam.suites:
        assert (s.kind, s.harness, s.extra['tier']) == ('judged', 'campaign', 'campaign')
        mod, fn = s.extra['verdict'].split(':')
        assert mod == 'verdicts' and callable(getattr(v, fn))
        assert s.matrix == {'machine.vcpus': [4]}
    t = fam.suite('torture').extra['expected']
    assert sorted(t) == sorted(f'{n}.{o}' for n in ('ieee_inf-2', 'ieee_inf-3', 'ieee_pr36332', 'pr39228') for o in ('O0', 'O2'))
    assert all('reduced precision' in why and 'docs/restructure/correctness-userspace.md' in why for why in t.values())


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
    (img / 'openssl-list.txt').write_text('just-a-name\n')
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
    time.sleep(0.5)
    alive = subprocess.run(['kill', '-0', str(child)], capture_output=True).returncode == 0
    assert not alive


def paths_root():
    from testkit import paths
    return paths.repo_root()
