"""The stress-ng campaign (plan 2026-11-18-campaign-onboarding, Task 10): its case list (tools/correctness/recipes/stress/
cases.py), its recipe's key (the container build stubbed) and the stress verdict's instance count. Nothing here boots
a VM or builds an image."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import textwrap

import pytest

from testkit import family, inputs, paths
from testkit.harness import campaign

REPO = paths.repo_root()
CT = REPO / 'tools' / 'correctness'
LIST = CT / 'stress-list.txt'


def _cases_module():
    spec = importlib.util.spec_from_file_location('stress_cases', CT / 'recipes' / 'stress' / 'cases.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _verdicts():
    return family._load_module(REPO / 'tests' / 'correctness' / 'verdicts.py', 'test_campaign_stress_verdicts')


def test_stress_cases_are_every_family_but_the_tails_once():
    c = _cases_module()
    got = c.cases(LIST.read_text())
    parts = list(dict.fromkeys(p for p, _ in got))
    fams = [p.split(',')[0] for p in parts]
    in_list = list(dict.fromkeys(f for f, _ in c.entries(LIST.read_text())))
    assert fams == [f for f in in_list if f not in c.TAILS] and set(c.TAILS) <= set(in_list)
    assert len(parts) == 18 and len(got) == 153
    names = [t for _, t in got]
    assert len(set(names)) == len(names)                                    # the harness's list names a test once
    assert all(re.fullmatch(r'[a-z0-9-]+(:[a-z0-9]+)?\.r1', t) for t in names)
    assert all(p == f'{p.split(",")[0]},seed=1,n={1 if p.startswith("mix,") else 4}' for p in parts)
    # each part lists its family's stressors in list order, as run-stress.sh runs them
    for p in parts:
        fam = p.split(',')[0]
        assert [t for q, t in got if q == p] == [f'{sl}.r1' for f, sl in c.entries(LIST.read_text()) if f == fam]
    assert ('shmx,seed=1,n=4', 'shm-sysv:root.r1') in got and ('ipc1,seed=1,n=4', 'shm-sysv.r1') in got


def test_stress_suite_parts_match_the_cases():
    fam = family.load(REPO / 'tests' / 'correctness' / 'family.toml')
    s = fam.suite('stress')
    got = _cases_module().cases(LIST.read_text())
    assert s.extra['parts'] == len(dict.fromkeys(p for p, _ in got)) == 18
    assert (s.kind, s.harness, s.extra['tier'], s.extra['cap'], s.extra['verdict']) == \
        ('judged', 'campaign', 'campaign', 1800, 'verdicts:stress')
    build = (CT / 'recipes' / 'stress' / 'build.sh').read_text()
    assert re.search(r'^PARTS=18\b', build, re.M)
    assert 'built.sha256' in build and not re.search(r'^[^#]*53140a75', build, re.M)   # recorded, never pinned


def test_stress_cases_refuse_a_name_listed_twice(tmp_path, capsys):
    c = _cases_module()
    lst = tmp_path / 'l.txt'
    lst.write_text('# x\nmem1 vm 10\nmem2 vm 20\n')
    try:
        c.main(['cases.py', str(lst)])
    except SystemExit as e:
        assert 'vm.r1' in str(e)
    else:
        raise AssertionError('a name listed twice was accepted')


def test_mix_counts_every_dispatched_instance():
    v = _verdicts()
    ok = {'rc': '0', 'n': '1', 'pass': '11', 'skip': '0', 'fail': '0', 'bogo': '5', 'sec': '241'}
    hogs = ['CT-LOG vm:mix1 | stress-ng: info:  [80] dispatching hogs: 1 vm, 1 mmapfork, 1 fork, 1 pthread, 1 icache, '
            '1 far-branch, 1 tlb-shootdown, 1 sigsegv, 1 switch, 1 atomic, 1 futex']
    r = v.stress('vm:mix1.r1', ok, hogs)
    assert r[0] == 'passed' and 'timeout' in r[1]
    r = v.stress('vm:mix1.r1', dict(ok, **{'pass': '10'}), hogs)            # one instance unaccounted for
    assert r[0] == 'failed' and 'dispatched 11' in r[1]
    one = ['CT-LOG vm | stress-ng: info:  [80] dispatching hogs: 4 vm']
    assert v.stress('vm.r1', dict(ok, n='4', **{'pass': '4'}), one)[0] == 'passed'
    assert v.stress('vm.r1', dict(ok, n='4', **{'pass': '3'}), one)[0] == 'failed'
    assert v.stress('vm.r1', dict(ok, n='4', **{'pass': '11'}), [])[0] == 'failed'   # no hogs line: n counts


def test_stress_campaign_image_key_changes_with_recipe(monkeypatch, tmp_path):
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    t = tmp_path / 'tools'
    for d in ('correctness', 'play'): shutil.copytree(inputs._tools() / d, t / d, ignore=shutil.ignore_patterns('__pycache__'))
    monkeypatch.setattr(inputs, '_tools', lambda: t)
    calls = []

    def fake(name, recipe, work, **kw):
        calls.append({p.relative_to(recipe).as_posix() for p in recipe.rglob('*') if p.is_file()})
        assert kw['script'] == 'correctness/recipes/stress/build.sh'
        (work / 'stress.cpio.zst').write_bytes(b'image')
        (work / 'stress-cases.txt').write_text(''.join(f'{p} {x}\n' for p, x in _cases_module().cases(LIST.read_text())))
        pins = inputs.read_pins(recipe / inputs.CAMPAIGN_RECIPES)
        (work / 'downloads.txt').write_text(''.join(f'http://snapshot.example/{f} {sha} {f}\n' for f, (sha, _) in sorted(pins.items())))
        (work / 'log').mkdir()
        (work / 'log' / 'built.sha256').write_text('ab' * 32 + '  stress-ng\n')
    monkeypatch.setattr(inputs, '_in_container', fake)

    first = inputs.campaign_image('stress')
    assert inputs.campaign_image('stress') == first and len(calls) == 1
    assert calls[0] >= {'correctness/recipes/stress/build.sh', 'correctness/recipes/stress/cases.py',
                        'correctness/recipes/stress/sources.sha256', 'correctness/stress-remote.sh',
                        'correctness/run-stress.sh', 'correctness/stress-list.txt', 'correctness/remote.sh',
                        'correctness/init', 'play/rootfs.sh', 'play/mkcpio.py'}
    assert not any(f.startswith(('correctness/recipes/openssl/', 'correctness/recipes/torture/')) for f in calls[0])
    m = json.loads((first / 'manifest.json').read_text())
    assert m['list']['tests'] == 153 and len(m['list']['parts']) == 18
    assert m['built'] == [{'file': 'stress-ng', 'sha256': 'ab' * 32}]                 # the binary's hash, recorded
    assert any(s['file'] == 'stress-ng_0.19.02.orig.tar.xz' and s['sha256'].startswith('ffe89071') for s in m['sources'])

    (t / 'correctness/run-ltp.sh').write_text('# not read by the stress recipe\n')
    assert inputs.campaign_image('stress') == first and len(calls) == 1
    for f in ('correctness/recipes/stress/build.sh', 'correctness/recipes/stress/cases.py', 'correctness/stress-list.txt',
              'correctness/run-stress.sh', 'correctness/stress-remote.sh', 'correctness/recipes/common.sh'):
        with open(t / f, 'a') as h: h.write('# changed\n')
        again = inputs.campaign_image('stress')
        assert again != first, f
        first = again
    assert len(calls) == 7
    assert campaign.read_list(first, 'stress')[0] == ('mem1,seed=1,n=4', [
        f'{sl}.r1' for f, sl in _cases_module().entries(LIST.read_text()) if f == 'mem1'])


def _boot_one(tmp_path, monkeypatch):
    """One part of a one-test stress suite with part_args = "scale=0.05,timeout=20", booted by a fake: (the part words
    booted, the case's result)."""
    d = tmp_path / 'tests' / 'c'
    d.mkdir(parents=True)
    shutil.copy(REPO / 'tests' / 'correctness' / 'verdicts.py', d / 'verdicts.py')
    (d / 'family.toml').write_text(textwrap.dedent('''\
        [family]
        name = "c"
        [[suite]]
        name = "stress"
        kind = "judged"
        harness = "campaign"
        tier = "campaign"
        parts = 1
        cap = 60
        verdict = "verdicts:stress"
        part_args = "scale=0.05,timeout=20"
        matrix = { "machine.vcpus" = [4] }
        '''))
    family.load.cache_clear()
    fam = family.load(d / 'family.toml')
    suite = fam.suite('stress')
    img = tmp_path / 'image'
    img.mkdir()
    (img / 'stress.cpio.zst').write_bytes(b'x')
    (img / 'stress-cases.txt').write_text('mem1,seed=1,n=4 vm.r1\n')
    monkeypatch.setattr(campaign, 'image', lambda name: img)
    calls = []

    def fake(image, s, part, vcpus, work, cap):
        calls.append(part)
        text = (f'CT-BEGIN suite=stress part={part} cpus=4\n'
                'CT-LOG vm | run-stress: ops=18442 timeout=20\n'
                'CT-RESULT stress vm.r1 rc=0 n=4 pass=4 skip=0 fail=0 bogo=18442 sec=20 to=20\n'
                f'CT-END suite=stress part={part} rc=0 guest_s=21\n')
        return campaign.BootResult(True, text)
    monkeypatch.setattr(campaign, 'boot', fake)
    return calls, campaign.HARNESS.run_part(fam, suite, 'part-0', family.expand(suite)[0], tmp_path / 'run')


def test_part_args_extend_the_booted_part_word(tmp_path, monkeypatch):
    """Suite key part_args: appended to the kernel's part= word; the list and the CT-END check keep the base word."""
    monkeypatch.delenv(campaign.PART_ARGS_ENV, raising=False)
    calls, res = _boot_one(tmp_path, monkeypatch)
    assert calls == ['mem1,seed=1,n=4,scale=0.05,timeout=20']
    assert res.outcome == 'passed', res.message
    assert [(s.name, s.outcome) for s in res.sub] == [('vm.r1', 'passed')]
    assert 'ran to the 20 s timeout' in res.sub[0].longrepr and 'scale=0.05,timeout=20' in res.message


def test_part_args_overridden_from_the_environment(tmp_path, monkeypatch):
    """TESTKIT_PART_ARGS replaces the suite's part_args for a run; empty means none (run-stress.sh's defaults)."""
    for env, want in (('scale=1,timeout=240', 'mem1,seed=1,n=4,scale=1,timeout=240'), ('', 'mem1,seed=1,n=4')):
        monkeypatch.setenv(campaign.PART_ARGS_ENV, env)
        calls, res = _boot_one(tmp_path / (env or 'none'), monkeypatch)
        assert calls == [want] and res.outcome == 'passed', res.message


def test_part_args_from_the_environment_can_name_their_suite(tmp_path, monkeypatch):
    """Review: a bare TESTKIT_PART_ARGS replaced part_args for every campaign suite of the run (stress's scale= went to
    litmus too). `SUITE:ARGS` entries, `;`-separated, apply to their suite only; a bare entry still applies to every
    suite no entry names (docs/testing.md promised it)."""
    for env, want in (('stress:scale=1,timeout=240', 'mem1,seed=1,n=4,scale=1,timeout=240'),
                      ('litmus:r=100', 'mem1,seed=1,n=4,scale=0.05,timeout=20'),
                      ('litmus:r=100;stress:scale=2', 'mem1,seed=1,n=4,scale=2'),
                      ('stress:', 'mem1,seed=1,n=4'),
                      ('scale=3;litmus:', 'mem1,seed=1,n=4,scale=3')):
        monkeypatch.setenv(campaign.PART_ARGS_ENV, env)
        calls, res = _boot_one(tmp_path / str(abs(hash(env))), monkeypatch)
        assert calls == [want] and res.outcome == 'passed', (env, res.message)


def test_part_args_from_the_environment_refuse_a_suite_named_twice():
    for bad in ('stress:scale=1;stress:scale=2', 'scale=1;scale=2'):
        with pytest.raises(ValueError, match='TESTKIT_PART_ARGS'):
            campaign.part_args('stress', '', bad)
    assert campaign.part_args('stress', 'a=1', None) == 'a=1'
    assert campaign.part_args('stress', 'a=1', 'x=1:2') == 'x=1:2'        # a colon after "=" is a value, not a suite
    # a trailing or doubled `;` names nothing: litmus keeps its own part_args (review: it lost its smoke size)
    for env in ('stress:scale=1;', 'stress:scale=1;;', ';stress:scale=1'):
        assert campaign.part_args('litmus', 'r=10,s=10000', env) == 'r=10,s=10000', env
        assert campaign.part_args('stress', '', env) == 'scale=1', env
    assert campaign.part_args('litmus', 'r=10', '') == '' and campaign.part_args('litmus', 'r=10', ' ; ') == ''


def test_stress_timeout_with_no_operation_fails():
    """A stressor that reaches its timeout having done nothing (a livelock) fails, whatever its pass count says."""
    v = _verdicts()
    ok = {'rc': '0', 'n': '4', 'pass': '4', 'skip': '0', 'fail': '0', 'bogo': '0', 'sec': '241', 'to': '240'}
    r = v.stress('futex.r1', ok, [])
    assert r[0] == 'failed' and 'no operation done' in r[1]
    assert v.stress('futex.r1', dict(ok, bogo='-'), [])[0] == 'failed'
    assert v.stress('futex.r1', dict(ok, bogo='12'), [])[0] == 'passed'                      # slow but working
    assert v.stress('futex.r1', dict(ok, sec='3'), [])[0] == 'passed'                        # op limit 0-ish, ended early
    skipped = dict(ok, **{'pass': '0', 'skip': '4', 'sec': '1'})
    assert v.stress('misaligned:int64wrds.r1', skipped, [])[0] == 'unsupported'


def test_root_shm_sysv_runs_are_expected_with_their_document():
    s = family.load(REPO / 'tests' / 'correctness' / 'family.toml').suite('stress')
    exp = s.extra['expected']
    assert sorted(exp) == ['shm-sysv.r1', 'shm-sysv:root.r1']
    assert all('docs/restructure/correctness-stress.md' in why for why in exp.values())
    names = {t for _, t in _cases_module().cases(LIST.read_text())}
    assert set(exp) <= names
    assert 'TIMEOUT_OK' not in (REPO / 'tests' / 'correctness' / 'verdicts.py').read_text()


def test_stress_timeout_note_follows_the_runs_timeout():
    v = _verdicts()
    ok = {'rc': '0', 'n': '4', 'pass': '4', 'skip': '0', 'fail': '0', 'bogo': '9', 'sec': '20', 'to': '20'}
    assert v.stress('vm.r1', ok, [])[1].endswith('(ran to the 20 s timeout)')
    assert 'timeout' not in v.stress('vm.r1', dict(ok, sec='19'), [])[1]
    assert 'timeout' not in v.stress('vm.r1', {k: x for k, x in ok.items() if k != 'to'}, [])[1]   # default: 240


def _run_stress(tmp_path, part):
    """tools/correctness/run-stress.sh with a fake stress-ng (prints its arguments and a passed line) and stand-ins for
    timeout(1) (records its duration), nproc and dmesg."""
    d, b = tmp_path / 'd', tmp_path / 'bin'
    d.mkdir()
    b.mkdir()
    (d / 'stress-list.txt').write_text('# c\nmem1 vm 368833 --vm-bytes 64M\nmem1 madvise 9\nmem2 brk 5\n')
    (d / 'stress-ng').write_text('#!/bin/sh\necho "stress-ng: info:  [1] args: $*"\necho "stress-ng: info:  [1] passed: 4: x (4)"\n')
    (b / 'timeout').write_text(f'#!/bin/sh\necho "$3" >> {tmp_path}/kill.txt\nshift 3\nexec "$@"\n')
    (b / 'nproc').write_text('#!/bin/sh\necho 4\n')
    (b / 'dmesg').write_text('#!/bin/sh\nexit 1\n')
    for f in (d / 'stress-ng', b / 'timeout', b / 'nproc', b / 'dmesg'): f.chmod(0o755)
    env = dict(os.environ, PATH=f'{b}:{os.environ["PATH"]}', TMPDIR=str(tmp_path))
    out = subprocess.run(['sh', str(CT / 'run-stress.sh'), str(d), part], capture_output=True, text=True, env=env,
                         check=True).stdout
    return out, (tmp_path / 'kill.txt').read_text().split()


def test_run_stress_scale_and_timeout(tmp_path):
    out, kills = _run_stress(tmp_path, 'mem1,seed=1,n=4,scale=0.05,timeout=20')
    assert '--vm-ops 18442 --verify --seed 1 --timeout 20 ' in out                     # round(368833 * 0.05)
    assert '--madvise-ops 1 --verify' in out                                            # round(0.45), at least 1
    assert 'CT-LOG vm | run-stress: ops=18442 timeout=20' in out and kills == ['180', '180']
    assert re.search(r'^CT-RESULT stress vm\.r1 rc=0 n=4 pass=4 skip=- fail=0 bogo=- sec=\d+ to=20$', out, re.M)
    assert 'brk' not in out


def test_run_stress_defaults_unchanged(tmp_path):
    out, kills = _run_stress(tmp_path, 'mem1,seed=2,n=4')
    assert '--vm-ops 368833 --verify --seed 2 --timeout 240 ' in out and '--madvise-ops 9 ' in out
    assert kills == ['400', '400'] and 'CT-LOG vm | run-stress: ops=368833 timeout=240' in out and ' to=240' in out
