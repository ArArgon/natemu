"""inputs.campaign_image (plan 2026-11-18-campaign-onboarding, Task 5), with the container build stubbed."""
import json
import shutil

import pytest

from testkit import inputs
from testkit.prepare import BuildFailure

SUITES = ('openssl', 'torture')


def _fake_build(calls, extra=(), drop=()):
    """A stand-in for the container: the outputs a recipe writes, every pinned file "downloaded" from a fake URL."""
    def fake(name, recipe, work, **kw):
        suite = kw['script'].split('/')[-2]
        calls.append((name, {p.relative_to(recipe).as_posix() for p in recipe.rglob('*') if p.is_file()}, kw))
        assert (recipe / kw['script']).is_file() and kw['timeout'] == inputs.CAMPAIGN_TIMEOUT
        (work / f'{suite}.cpio.zst').write_bytes(b'image')
        (work / f'{suite}-list.txt').write_text('0/2 default:test_a\n1/2 default:test_b\n')
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
    assert (d / f'{suite}.cpio.zst').is_file() and (d / f'{suite}-list.txt').is_file() and (d / 'log/build.log').is_file()
    assert not (d / 'src').exists() and not (d / 'work').exists()
    if suite == 'openssl': assert any(f.startswith('openssl_3.5.7.orig.tar') for f in pinned)
    else: assert any(f.startswith('gcc-14_14.2.0.orig.tar') for f in pinned)


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
