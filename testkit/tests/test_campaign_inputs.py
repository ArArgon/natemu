"""inputs.campaign_image (plan 2026-11-18-campaign-onboarding, Task 5), with the container build stubbed."""
import json
import shutil

import pytest

from testkit import inputs
from testkit.prepare import BuildFailure

SUITES = ('openssl', 'torture', 'libm', 'gstr', 'x86st')
ORIG = {'openssl': 'openssl_3.5.7.orig.tar', 'torture': 'gcc-14_14.2.0.orig.tar', 'libm': 'glibc_2.41.orig.tar',
        'gstr': 'glibc_2.41.orig.tar', 'x86st': 'linux_6.12.107.orig.tar'}


def _fake_build(calls, extra=(), drop=()):
    """A stand-in for the container: the outputs a recipe writes, every pinned file "downloaded" from a fake URL."""
    def fake(name, recipe, work, **kw):
        suite = kw['script'].split('/')[-2]
        calls.append((name, {p.relative_to(recipe).as_posix() for p in recipe.rglob('*') if p.is_file()}, kw))
        assert (recipe / kw['script']).is_file() and kw['timeout'] == inputs.CAMPAIGN_TIMEOUT
        (work / f'{suite}.cpio.zst').write_bytes(b'image')
        (work / f'{suite}-cases.txt').write_text('0/2 default:test_a\n1/2 default:test_b\n')
        pins = inputs.read_pins(recipe / inputs.CAMPAIGN_RECIPES)
        lines = [f'http://snapshot.example/{f} {sha} {f}' for f, (sha, _) in sorted(pins.items()) if f not in drop]
        (work / 'downloads.txt').write_text(''.join(l + '\n' for l in [*lines, *extra]))
        (work / 'log').mkdir(); (work / 'log' / 'build.log').write_text('ok\n')
    return fake


def _tools_copy(tmp_path):
    t = tmp_path / 'tools'
    for d in ('correctness', 'play'): shutil.copytree(inputs._tools() / d, t / d, ignore=shutil.ignore_patterns('__pycache__'))
    return t


def test_campaign_image_key_changes_with_recipe(monkeypatch, tmp_path):
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    t = _tools_copy(tmp_path)
    monkeypatch.setattr(inputs, '_tools', lambda: t)
    calls = []
    monkeypatch.setattr(inputs, '_in_container', _fake_build(calls))
    first = inputs.campaign_image('openssl')
    assert inputs.campaign_image('openssl') == first and len(calls) == 1            # cached
    staged = calls[0][1]
    assert staged >= {'correctness/recipes/openssl/build.sh', 'correctness/recipes/common.sh', 'correctness/init',
                      'correctness/remote.sh', 'correctness/run-openssl.sh', 'play/rootfs.sh', 'play/mkcpio.py'}
    assert 'correctness/run-ltp.sh' not in staged and not any(f.startswith('correctness/recipes/torture/') for f in staged)

    (t / 'correctness/run-ltp.sh').write_text('# not read by the openssl recipe\n')
    assert inputs.campaign_image('openssl') == first and len(calls) == 1
    for f in ('correctness/recipes/openssl/build.sh', 'correctness/recipes/common.sh', 'correctness/init', 'correctness/remote.sh'):
        with open(t / f, 'a') as h: h.write('# changed\n')
        again = inputs.campaign_image('openssl')
        assert again != first, f
        first = again
    assert len(calls) == 5
    monkeypatch.setattr(inputs.artifacts, 'BENCH_BUILDER', 'docker.io/library/debian@sha256:' + '0' * 64)
    assert inputs.campaign_image('openssl') != first


# What each Task 8 recipe reads besides remote.sh and its own folder (its uses.txt), and a file it must not be keyed by.
OWN = {'libm': (['correctness/libm.py', 'correctness/run-libm.sh'], 'correctness/gstr.py'),
       'gstr': (['correctness/gstr.py', 'correctness/cpy-remote.sh', 'correctness/run-gstr.sh'], 'correctness/libm.py'),
       'x86st': (['correctness/x86-remote.sh', 'correctness/run-x86st.sh', 'correctness/x86st-skip.txt'], 'correctness/libm.py'),
       'ltp32': (['correctness/i386.sh', 'correctness/ltp-list.py', 'correctness/run-ltp.sh', 'correctness/recipes/ltp/cases.sh'],
                 'correctness/libm.py'),
       'libm32': (['correctness/i386.sh', 'correctness/libm.py', 'correctness/run-libm.sh'], 'correctness/gstr.py')}


@pytest.mark.parametrize('suite', sorted(OWN))
def test_new_recipe_key_follows_its_files(monkeypatch, tmp_path, suite):
    """Stubbed build: the image is keyed by the recipe's build.sh, its pins and the files its uses.txt names (the
    scripts its build runs and the files its image carries), and not by another suite's."""
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    t = _tools_copy(tmp_path)
    monkeypatch.setattr(inputs, '_tools', lambda: t)
    calls = []
    monkeypatch.setattr(inputs, '_in_container', _fake_build(calls))
    own, other = OWN[suite]
    first = inputs.campaign_image(suite)
    staged = calls[0][1]
    assert staged >= {f'correctness/recipes/{suite}/build.sh', f'correctness/recipes/{suite}/sources.sha256',
                      'correctness/recipes/common.sh', 'correctness/remote.sh', 'correctness/run-repro.sh', *own}
    assert other not in staged and not any(f.startswith('correctness/recipes/openssl/') for f in staged)
    with open(t / other, 'a') as h: h.write('# not read by this recipe\n')
    assert inputs.campaign_image(suite) == first and len(calls) == 1
    for f in (f'correctness/recipes/{suite}/build.sh', f'correctness/recipes/{suite}/sources.sha256', *own):
        text = (t / f).read_text()          # a pin file changes by a pin (it has no comments), the rest by a comment
        (t / f).write_text('0' * 64 + text[64:] if f.endswith('.sha256') else text + '# changed\n')
        again = inputs.campaign_image(suite)
        assert again != first, f
        first = again


def test_x86st_skip_list_is_names_with_reasons():
    """run-x86st.sh matches x86st-skip.txt's lines whole (grep -qx): an entry is a bare program name, with its reason in
    the comment right above it, and names a program the image has (x86st-cases.txt, checked at run time as stale)."""
    lines = (inputs._tools() / 'correctness' / 'x86st-skip.txt').read_text().splitlines()
    assert lines and lines[0].startswith('#')
    for i, line in enumerate(lines):
        if not line.strip() or line.startswith('#'): continue
        assert line.strip() == line and ' ' not in line and line.endswith('_64'), line
        assert lines[i - 1].startswith('# ') and len(lines[i - 1]) > 4, f'{line}: no reason above it'


def test_unknown_suite_has_no_recipe():
    with pytest.raises(BuildFailure, match='no recipe'):
        inputs.campaign_image('no-such-suite')


@pytest.mark.parametrize('suite', SUITES)
def test_manifest_lists_every_download_with_sha256(monkeypatch, tmp_path, suite):
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    folder = inputs._tools() / inputs.CAMPAIGN_RECIPES
    pin_files = sorted([*folder.glob('common/*.sha256'), *folder.glob(f'{suite}/*.sha256')])
    names = {p.relative_to(folder).as_posix() for p in pin_files}
    assert {'common/tools-debs.sha256', 'common/sysroot-debs.sha256', 'common/play-debs.sha256', 'common/kernel-deb.sha256',
            f'{suite}/sources.sha256'} <= names
    pinned = {}
    for p in pin_files:
        for line in p.read_text().splitlines():
            m = inputs.PIN_LINE.fullmatch(line)
            assert m, f'{p}: {line!r}'
            pinned[m[2]] = m[1]
    calls = []
    monkeypatch.setattr(inputs, '_in_container', _fake_build(calls))
    d = inputs.campaign_image(suite)
    m = json.loads((d / 'manifest.json').read_text())
    assert {s['file']: s['sha256'] for s in m['sources']} == pinned
    assert all(s['url'].startswith('http') and len(s['sha256']) == 64 and s['pins'] for s in m['sources'])
    assert m['image']['file'] == f'{suite}.cpio.zst' and m['list'] == dict(m['list'], tests=2, parts=['0/2', '1/2'])
    assert (d / f'{suite}.cpio.zst').is_file() and (d / f'{suite}-cases.txt').is_file() and (d / 'log/build.log').is_file()
    assert not (d / 'src').exists() and not (d / 'work').exists()
    assert any(f.startswith(ORIG[suite]) for f in pinned)


def test_cpython_suites_are_built_by_the_cpy_recipe(monkeypatch, tmp_path):
    """cpyt and cpyb are built by recipes/cpy (their own folders only name the suite and source it): each image stages
    that recipe and is keyed by it, and each is its own entry with its own image and list."""
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    t = _tools_copy(tmp_path)
    monkeypatch.setattr(inputs, '_tools', lambda: t)
    calls = []
    monkeypatch.setattr(inputs, '_in_container', _fake_build(calls))
    first = {s: inputs.campaign_image(s) for s in ('cpyt', 'cpyb')}
    assert first['cpyt'] != first['cpyb'] and len(calls) == 2
    for (name, staged, kw), suite in zip(calls, ('cpyt', 'cpyb')):
        assert name == f'campaign-{suite}' and kw['script'] == f'correctness/recipes/{suite}/build.sh'
        assert staged >= {f'correctness/recipes/{suite}/build.sh', 'correctness/recipes/cpy/build.sh',
                          'correctness/recipes/cpy/smoke.txt', 'correctness/recipes/common.sh', 'correctness/init',
                          'correctness/remote.sh', 'correctness/cpy-remote.sh', 'correctness/cpy-build.sh',
                          'correctness/run-cpy.sh', 'correctness/run-cpyb.sh', 'correctness/run-repro.sh',
                          'play/rootfs.sh', 'play/mkcpio.py'}, suite
        assert not any(f.startswith(('correctness/recipes/openssl/', 'correctness/recipes/torture/')) for f in staged)
        wrapper = (t / f'correctness/recipes/{suite}/build.sh').read_text()
        assert f'SUITE={suite}\n' in wrapper and '. /recipe/correctness/recipes/cpy/build.sh' in wrapper
        assert (first[suite] / f'{suite}.cpio.zst').is_file() and (first[suite] / f'{suite}-cases.txt').is_file()
    for f in ('correctness/recipes/cpy/build.sh', 'correctness/recipes/cpy/smoke.txt', 'correctness/cpy-remote.sh'):
        with open(t / f, 'a') as h: h.write('# changed\n')
        again = {s: inputs.campaign_image(s) for s in ('cpyt', 'cpyb')}
        assert again['cpyt'] != first['cpyt'] and again['cpyb'] != first['cpyb'], f
        first = again
    with open(t / 'correctness/recipes/cpyb/build.sh', 'a') as h: h.write('# changed\n')
    assert inputs.campaign_image('cpyt') == first['cpyt'] and inputs.campaign_image('cpyb') != first['cpyb']


def test_unpinned_download_fails(monkeypatch, tmp_path):
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    monkeypatch.setattr(inputs, '_in_container', _fake_build([], extra=['http://x/evil.deb ' + 'a' * 64 + ' evil.deb']))
    with pytest.raises(BuildFailure, match='evil.deb .* no pin file names it'):
        inputs.campaign_image('torture')


def test_pin_never_downloaded_fails(monkeypatch, tmp_path):
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    k = 'linux-image-6.12.107+deb13-amd64_6.12.107-1_amd64.deb'
    monkeypatch.setattr(inputs, '_in_container', _fake_build([], drop={k}))
    with pytest.raises(BuildFailure, match='pinned but not downloaded'):
        inputs.campaign_image('torture')


def test_repin_tree_is_kept_and_named(monkeypatch, tmp_path):
    """A re-pinning build's new pin files (work/repin/...) survive the failed entry, with their folders."""
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    monkeypatch.setattr(inputs.paths, 'state_dir', lambda: tmp_path / 'state')
    monkeypatch.setattr(inputs.shutil, 'which', lambda n: '/usr/bin/true')
    work = tmp_path / 'w'; (work / 'repin/openssl').mkdir(parents=True)
    (work / 'repin/openssl/sources.sha256').write_text('x\n')
    monkeypatch.setattr(inputs.subprocess, 'run', lambda *a, **k: inputs.subprocess.CompletedProcess(a, 1, '', 'new pins\n'))
    with pytest.raises(BuildFailure, match='openssl/sources.sha256'):
        inputs._in_container('campaign-openssl', tmp_path, work, script='b.sh', pins_to=tmp_path / 'recipes')
    assert (tmp_path / 'state/repin/campaign-openssl/openssl/sources.sha256').read_text() == 'x\n'


def _with_boot_file(boot, kernel=True):
    inner = _fake_build([])

    def fake(name, recipe, work, **kw):
        inner(name, recipe, work, **kw)
        (work / 'openssl-boot.txt').write_text(boot)
        if kernel: (work / 'openssl-kernel').write_bytes(b'bzImage')
    return fake


def test_boot_file_and_its_kernels_are_kept(monkeypatch, tmp_path):
    """A recipe's <suite>-boot.txt (per-part kernels, public) and the kernels it names reach the entry and its manifest."""
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    monkeypatch.setattr(inputs, '_in_container', _with_boot_file('0/2 pubcap=300\n1/2 kernel=openssl-kernel pubcap=300\n'))
    d = inputs.campaign_image('openssl')
    m = json.loads((d / 'manifest.json').read_text())
    assert set(m['boot']) == {'openssl-boot.txt', 'openssl-kernel'} and (d / 'openssl-kernel').read_bytes() == b'bzImage'


def test_boot_file_naming_a_kernel_not_built_fails(monkeypatch, tmp_path):
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    monkeypatch.setattr(inputs, '_in_container', _with_boot_file('0/2 kernel=openssl-kernel\n', kernel=False))
    with pytest.raises(BuildFailure, match='names kernels the recipe did not write'):
        inputs.campaign_image('openssl')


def _litmus_build(calls):
    """_fake_build plus the oracle folder the litmus recipe writes (herd7's states per family)."""
    base = _fake_build(calls)

    def fake(name, recipe, work, **kw):
        base(name, recipe, work, **kw)
        (work / 'oracle').mkdir(); (work / 'oracle' / 'cat86.txt').write_text('Test SB Allowed\nStates 1\n0:EAX=1;\nOk\n')
    return fake


def test_litmus_image_key_changes_with_recipe(monkeypatch, tmp_path):
    """The litmus image is keyed by its recipe and the x86 lane's scripts it runs; the oracle stays in its folder."""
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    t = _tools_copy(tmp_path)
    monkeypatch.setattr(inputs, '_tools', lambda: t)
    calls = []
    monkeypatch.setattr(inputs, '_in_container', _litmus_build(calls))
    first = inputs.campaign_image('litmus')
    assert inputs.campaign_image('litmus') == first and len(calls) == 1
    staged = calls[0][1]
    assert staged >= {'correctness/recipes/litmus/build.sh', 'correctness/recipes/litmus/sources.sha256',
                      'correctness/recipes/litmus/ocaml-debs.sha256', 'correctness/recipes/common.sh', 'correctness/init',
                      'correctness/remote.sh', 'correctness/x86-remote.sh', 'correctness/litmus-gen.py',
                      'correctness/run-litmus.sh', 'play/rootfs.sh', 'play/mkcpio.py'}
    assert 'correctness/litmus-check.py' not in staged          # the verdict's, read on the Mac
    with open(t / 'correctness/litmus-check.py', 'a') as h: h.write('# not read by the recipe\n')
    assert inputs.campaign_image('litmus') == first and len(calls) == 1
    m = json.loads((first / 'manifest.json').read_text())
    assert (first / 'oracle' / 'cat86.txt').is_file() and set(m['oracle']) == {'cat86.txt'} and len(m['oracle']['cat86.txt']) == 64
    for f in ('correctness/recipes/litmus/build.sh', 'correctness/x86-remote.sh', 'correctness/litmus-gen.py',
              'correctness/run-litmus.sh'):
        with open(t / f, 'a') as h: h.write('# changed\n')
        again = inputs.campaign_image('litmus')
        assert again != first, f
        first = again
    assert len(calls) == 5


def test_litmus_manifest_pins_herdtools_catalogue_and_ocaml(monkeypatch, tmp_path):
    """Every litmus source is pinned: herdtools7 7.58's tag archive, the 51 catalogue files of its commit,
    litmus-tests-x86 a6dadce's archive and the 14 OCaml packages, besides the common pins."""
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    monkeypatch.setattr(inputs, '_in_container', _litmus_build([]))
    m = json.loads((inputs.campaign_image('litmus') / 'manifest.json').read_text())
    files = {s['file']: s['pins'] for s in m['sources']}
    assert files['herdtools7-7.58.tar.gz'] == ['litmus/sources.sha256']
    assert files['litmus-tests-x86-a6dadced36343b9c21f00a173950a23e90068cd0.tar.gz'] == ['litmus/sources.sha256']
    cat = [f for f in files if f.startswith('herdtools7-catalogue-')]
    assert len([f for f in cat if f.startswith('herdtools7-catalogue-x86_64-')]) == 28 and len(cat) == 23 + 28
    assert sum(p == ['litmus/ocaml-debs.sha256'] for p in files.values()) == 14 and 'ocaml_5.3.0-3_amd64.deb' in files
