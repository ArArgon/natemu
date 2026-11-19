"""The LTP campaign (plan 2026-11-18-campaign-onboarding, Task 9): its recipe's case list against run-ltp.sh's own
split, the recipe's key and manifest with the container build stubbed, and the suite's entries in
tests/correctness/family.toml. Nothing here builds an image or starts a VM."""
import json
import os
import re
import subprocess

import pytest

from testkit import family, inputs, paths
from testkit.tests.test_campaign_harness import verdicts
from testkit.tests.test_campaign_inputs import _fake_build, _tools_copy

REPO = paths.repo_root()
CT = REPO / 'tools' / 'correctness'
DOC = 'docs/restructure/correctness-userspace.md'


def _list(tmp_path, n=11):
    """A small ltp-list.txt (RUNTEST<TAB>NAME<TAB>COMMAND) over two runtest files, one entry a shell assignment."""
    rows = [('syscalls' if i < 7 else 'mm', f't{i:02d}', 'true' if i != 3 else 'X=1; exit 32') for i in range(n)]
    f = tmp_path / 'ltp-list.txt'
    f.write_text(''.join('\t'.join(r) + '\n' for r in rows))
    return f, rows


def test_ltp_cases_follow_run_ltp_split(tmp_path):
    """ltp-cases.txt names, for each part word K/4, exactly the entries run-ltp.sh K/4 reports, as it names them."""
    lst, rows = _list(tmp_path)
    cases = subprocess.run(['sh', str(CT / 'recipes/ltp/cases.sh'), str(lst), '4'], capture_output=True, text=True, check=True).stdout
    listed = [l.split(' ', 1) for l in cases.splitlines()]
    assert len(listed) == len(rows) and len({t for _, t in listed}) == len(rows)
    assert [p for p, _ in listed][:5] == ['0/4', '1/4', '2/4', '3/4', '0/4']
    stub = tmp_path / 'bin'; stub.mkdir()               # run-ltp.sh's `timeout -s KILL N sh -c CMD`, without the timer
    (stub / 'timeout').write_text('#!/bin/sh\nshift 3\nexec "$@"\n'); (stub / 'timeout').chmod(0o755)
    root = tmp_path / 'ltp'; (root / 'testcases/bin').mkdir(parents=True)
    env = dict(os.environ, PATH=f'{stub}:{os.environ["PATH"]}', LTPROOT=str(root), TMPDIR=str(tmp_path))
    for k in range(4):
        out = subprocess.run(['sh', str(CT / 'run-ltp.sh'), str(tmp_path), f'{k}/4'], capture_output=True, text=True,
                             env=env, check=True).stdout
        ran = [m[1] for m in re.finditer(r'^CT-RESULT ltp (\S+) rc=(\d+)', out, re.M)]
        assert ran == [t for p, t in listed if p == f'{k}/4'], k
    assert ('1/4', 'syscalls:t01') in [tuple(x) for x in listed] and ('2/4', 'mm:t10') in [tuple(x) for x in listed]


def test_ltp_recipe_key(monkeypatch, tmp_path):
    """The LTP image is keyed by its recipe and the tools it reads (stubbed container build), nothing else."""
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    t = _tools_copy(tmp_path)
    monkeypatch.setattr(inputs, '_tools', lambda: t)
    calls = []
    monkeypatch.setattr(inputs, '_in_container', _fake_build(calls))
    first = inputs.campaign_image('ltp')
    assert inputs.campaign_image('ltp') == first and len(calls) == 1
    staged = calls[0][1]
    assert staged >= {'correctness/recipes/ltp/build.sh', 'correctness/recipes/ltp/cases.sh', 'correctness/recipes/ltp/sources.sha256',
                      'correctness/recipes/common.sh', 'correctness/init', 'correctness/remote.sh', 'correctness/run-ltp.sh',
                      'correctness/ltp-list.py', 'correctness/ltp.sha256', 'play/rootfs.sh', 'play/mkcpio.py'}
    assert calls[0][2]['script'] == 'correctness/recipes/ltp/build.sh'
    assert 'correctness/run-openssl.sh' not in staged and not any(f.startswith('correctness/recipes/openssl/') for f in staged)
    (t / 'correctness/run-openssl.sh').write_text('# not read by the ltp recipe\n')
    assert inputs.campaign_image('ltp') == first and len(calls) == 1
    for f in ('correctness/recipes/ltp/build.sh', 'correctness/recipes/ltp/cases.sh', 'correctness/ltp-list.py',
              'correctness/run-ltp.sh', 'correctness/remote.sh'):
        with open(t / f, 'a') as h: h.write('# changed\n')
        again = inputs.campaign_image('ltp')
        assert again != first, f
        first = again
    assert len(calls) == 6


def test_ltp_manifest_pins_the_upstream_release(monkeypatch, tmp_path):
    """The one non-Debian download, ltp-full-20250530, is pinned by the recipe with remote.sh's own hash."""
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    monkeypatch.setattr(inputs, '_in_container', _fake_build([]))
    m = json.loads((inputs.campaign_image('ltp') / 'manifest.json').read_text())
    src = {s['file']: s for s in m['sources']}
    assert src['ltp-full-20250530.tar.xz']['sha256'] == (CT / 'ltp.sha256').read_text().strip()
    assert src['ltp-full-20250530.tar.xz']['pins'] == ['ltp/sources.sha256']
    assert 'ltp.sha256' in (CT / 'remote.sh').read_text() and 'ltp-full-20250530' in (CT / 'recipes/ltp/build.sh').read_text()


def test_ltp_suite_entries():
    family.load.cache_clear()
    fam = family.load(REPO / 'tests' / 'correctness' / 'family.toml')
    s = fam.suite('ltp')
    assert (s.kind, s.harness, s.extra['tier'], s.extra['parts'], s.extra['verdict']) == ('judged', 'campaign', 'campaign', 4, 'verdicts:ltp')
    assert s.matrix == {'machine.vcpus': [4]}
    assert 'PARTS=4' in (CT / 'recipes/ltp/build.sh').read_text()
    assert sorted(s.extra['flaky']) == ['syscalls:getrusage04', 'syscalls:kill11', 'syscalls:pselect01_64']
    ex = s.extra['expected']
    assert len(ex) == 34 and all(re.fullmatch(r'(syscalls|cve):[\w-]+', n) for n in ex)
    assert 'syscalls:getcwd04' not in ex                       # TCONF at 1 vCPU only, and 1 vCPU is not run
    assert all(DOC in why for why in [*ex.values(), *s.extra['flaky'].values()])
    assert not set(ex) & set(s.extra['flaky'])


@pytest.mark.parametrize('rc,verdict', [('0', 'passed'), ('32', 'unsupported'), ('36', 'failed'), ('2', 'failed'),
                                         ('127', 'failed'), ('137', 'failed')])
def test_ltp_rc_including_twarn_tconf(rc, verdict):
    """TWARN+TCONF (4|32) is not TCONF: it fails, and the 10 module-metadata entries are listed as expected for it."""
    assert verdicts().ltp('syscalls:ioctl_loop01', {'rc': rc, 'sec': '0.1'}, [])[0] == verdict
