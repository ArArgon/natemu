import pathlib
import subprocess

import pytest

from testkit import artifacts, paths


def test_source_key_changes_with_uncommitted_edit(tmp_path, monkeypatch):
    g = lambda *a: subprocess.run(['git', '-C', str(tmp_path), *a], check=True, capture_output=True)
    g('init', '-q'); (tmp_path / 'src').mkdir(); (tmp_path / 'src' / 'a.rs').write_text('fn a() {}\n')
    g('add', '.'); g('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'x')
    monkeypatch.setattr(paths, 'repo_root', lambda: tmp_path)
    k0 = artifacts.source_key(['src'])
    assert artifacts.source_key(['src']) == k0
    (tmp_path / 'src' / 'a.rs').write_text('fn a() { 1; }\n')
    k1 = artifacts.source_key(['src'])
    assert k1 != k0
    (tmp_path / 'src' / 'new.rs').write_text('')
    assert artifacts.source_key(['src']) not in (k0, k1)
    assert artifacts.source_key(['other']) != k0


@pytest.mark.slow
def test_natemu_layout():
    n = artifacts.natemu()
    for p in (n.host, n.stub, n.runtime):
        assert p.is_file(), p
    for p in (n.host, n.stub):
        assert subprocess.run(['codesign', '-d', p], capture_output=True).returncode == 0


@pytest.mark.slow
def test_qemu_user_pinned():
    assert artifacts.qemu_user().name == 'qemu-x86_64-static'


@pytest.mark.slow
def test_nextest_runs():
    out = subprocess.run([artifacts.nextest(), 'nextest', '--version'], capture_output=True, text=True)
    assert out.returncode == 0 and '0.9.150' in out.stdout


@pytest.mark.slow
def test_qemu_system_pinned():
    b = artifacts.qemu_system()
    out = subprocess.run([b / 'qemu-system-x86_64', '--version'], capture_output=True, text=True)
    assert out.returncode == 0 and 'version 11.1.2' in out.stdout, out.stdout
    assert (b / 'qemu-img').is_file()
    share = b.parent / 'share' / 'qemu'
    assert (share / 'bios-256k.bin').is_file()
    # QEMU looks for its firmware there even though the entry was moved into place after `make install` (relocatable)
    dirs = subprocess.run([b / 'qemu-system-x86_64', '-L', 'help'], capture_output=True, text=True).stdout
    assert share.resolve() in {pathlib.Path(l.strip()).resolve() for l in dirs.splitlines() if l.strip()}, dirs
