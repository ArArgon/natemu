import os
import pathlib
import stat

import pytest

from testkit import artifacts
from testkit.prepare import BuildFailure


def test_builder_setting(monkeypatch, tmp_path):
    monkeypatch.delenv('NATEMU_BENCH_BUILDER', raising=False)
    monkeypatch.setattr(artifacts, 'local_config', lambda: {})
    assert artifacts.bench_builder() == 'local'
    monkeypatch.setattr(artifacts, 'local_config', lambda: {'bench': {'builder': 'ssh:me@box'}})
    assert artifacts.bench_builder() == 'ssh:me@box'
    monkeypatch.setenv('NATEMU_BENCH_BUILDER', 'local')
    assert artifacts.bench_builder() == 'local'
    monkeypatch.setenv('NATEMU_BENCH_BUILDER', 'docker')
    with pytest.raises(BuildFailure):
        artifacts.bench_builder()


def test_ssh_builder_runs_recipe_and_cleans_up(tmp_path):
    """A stand-in ssh/scp that runs commands locally: the recipe is copied, build.sh runs, outputs come back, and the
    remote temporary folder is removed."""
    remote = tmp_path / 'remote'; remote.mkdir()
    log = tmp_path / 'ssh.log'
    fake_ssh = tmp_path / 'ssh'
    fake_ssh.write_text(f'#!/bin/sh\n# skip -o OPT pairs and the host\nwhile [ "$1" = -o ]; do shift 2; done; shift\n'
                        f'echo "$@" >> {log}\nexport TMPDIR={remote}\nexec sh -c "$(echo "$@" | sed "s#mktemp -d /tmp/#mktemp -d {remote}/#")"\n')
    fake_scp = tmp_path / 'scp'
    fake_scp.write_text('#!/bin/sh\nwhile [ "$1" = -q ] || [ "$1" = -o ]; do [ "$1" = -o ] && shift; shift; done\n'
                        'cp "${1#*:}" "$2"\n')
    for f in (fake_ssh, fake_scp): f.chmod(f.stat().st_mode | stat.S_IEXEC)
    recipe = tmp_path / 'recipe' / 'scripts'; recipe.mkdir(parents=True)
    (recipe / 'build.sh').write_text('mkdir -p "$1/out"; echo cpio > "$1/out/bench.cpio"; echo exp > "$1/out/expected.txt"\n')
    out = tmp_path / 'out'; out.mkdir()
    artifacts._build_bench_ssh(recipe.parent, out, 'me@box', ssh=str(fake_ssh), scp=str(fake_scp))
    assert (out / 'bench.cpio').read_text() == 'cpio\n' and (out / 'expected.txt').read_text() == 'exp\n'
    assert list(remote.iterdir()) == []                         # nothing left on the builder
    assert 'rm -rf' in log.read_text()
