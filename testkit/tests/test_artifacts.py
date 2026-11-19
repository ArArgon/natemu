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


def _repo_with_same_tree_twice(tmp_path):
    """A repository with three commits: A, B (no change: the same tree as A) and C (a changed file)."""
    g = lambda *a: subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.email=t@t', '-c', 'user.name=t', *a],
                                  check=True, capture_output=True, text=True).stdout.strip()
    g('init', '-q'); (tmp_path / 'crates').mkdir(); (tmp_path / 'crates' / 'a.rs').write_text('fn a() {}\n')
    g('add', '.'); g('commit', '-qm', 'A'); a = g('rev-parse', 'HEAD')
    g('commit', '-q', '--allow-empty', '-m', 'B'); b = g('rev-parse', 'HEAD')
    (tmp_path / 'crates' / 'a.rs').write_text('fn a() { 1; }\n'); g('commit', '-qam', 'C'); c = g('rev-parse', 'HEAD')
    return a, b, c


def test_natemu_at_keys_by_tree(tmp_path, monkeypatch):
    """Two revisions with the same tree are one baseline build; a changed tree is another (build steps stubbed)."""
    repo = tmp_path / 'repo'; repo.mkdir()
    a, b, c = _repo_with_same_tree_twice(repo)
    monkeypatch.setattr(paths, 'repo_root', lambda: repo)
    monkeypatch.delenv('TESTKIT_ARTIFACTS', raising=False)
    keys = []
    def get_or_build(kind, k, build, **kw):
        keys.append((kind, k)); return tmp_path / 'entry' / k
    monkeypatch.setattr(artifacts.cache, 'get_or_build', get_or_build)
    monkeypatch.setattr(artifacts, 'pin_for_run', lambda *a: None)
    na, nb, nc = artifacts.natemu_at(a), artifacts.natemu_at(b[:8]), artifacts.natemu_at(c)
    assert keys[0] == keys[1] and keys[0][0] == 'natemu-at'
    assert keys[2] != keys[0]
    assert na == nb and isinstance(na, artifacts.Natemu) and na.stub == na.entry / 'release/natemu'
    assert nc.key != na.key


def test_natemu_at_builds_from_the_archive(tmp_path, monkeypatch):
    """The build runs cargo in a scratch folder holding `git archive REV` (never the worktree), installs the binaries
    in the target-folder layout, signs the two host binaries and removes the scratch folder."""
    repo = tmp_path / 'repo'; repo.mkdir()
    a, b, c = _repo_with_same_tree_twice(repo)
    (repo / 'crates' / 'a.rs').write_text('uncommitted\n')
    monkeypatch.setattr(paths, 'repo_root', lambda: repo)
    monkeypatch.setattr(paths, 'cache_root', lambda: tmp_path / 'cache')
    monkeypatch.delenv('TESTKIT_ARTIFACTS', raising=False)
    calls = []
    def run(cmd, **kw):
        cmd = [str(x) for x in cmd]; calls.append((cmd, kw.get('cwd')))
        if cmd[0] == 'cargo':
            assert pathlib.Path(kw['cwd'], 'crates/a.rs').read_text() == 'fn a() {}\n'      # A's content, not the worktree's
            t = pathlib.Path(kw['env']['CARGO_TARGET_DIR'])
            for rel in artifacts.NATEMU_BINARIES:
                (t / rel).parent.mkdir(parents=True, exist_ok=True); (t / rel).write_text(rel)
    monkeypatch.setattr(artifacts, '_run', run)
    n = artifacts.natemu_at(a)
    assert n.stub.read_text() == 'release/natemu' and n.runtime.is_file() and n.host.is_file()
    cargo = [c for c in calls if c[0][0] == 'cargo']
    assert cargo and all(pathlib.Path(cwd) != repo for _, cwd in cargo)
    signed = [c[0][-1] for c in calls if c[0][0] == 'codesign']
    assert sorted(pathlib.Path(s).name for s in signed) == ['natemu', 'natemu-host']
    assert not any((tmp_path / 'cache' / 'build-tmp').glob('natemu-at-*'))
    assert artifacts.natemu_at(b) == n            # pinned for the run: the same build without rebuilding


SECRET = 's3cr3t-token-value'


def test_a_missing_build_tool_is_a_build_failure_without_the_environment(tmp_path):
    """Review: an OSError (tool missing) or a timeout escaped _run(cmd, env=...) and preparation as a pytest
    INTERNALERROR, whose traceback lists each frame's arguments: the environment, tokens included."""
    from testkit.prepare import BuildFailure
    env = dict(__import__('os').environ, TESTKIT_SECRET=SECRET)
    with pytest.raises(BuildFailure) as e:
        artifacts._run([str(tmp_path / 'no-such-tool'), 'build'], env=env)
    assert 'no-such-tool' in str(e.value) and SECRET not in str(e.value)
    assert e.value.__suppress_context__ and e.value.__cause__ is None
    with pytest.raises(BuildFailure) as e:
        artifacts._run(['sleep', '5'], env=env, timeout=0.2)
    assert 'sleep' in str(e.value) and 'timed out' in str(e.value) and SECRET not in str(e.value)


def test_preparation_turns_any_error_into_a_build_failure(monkeypatch, tmp_path):
    import os, subprocess
    from types import SimpleNamespace
    from testkit import prepare
    from testkit.harness import Requirement
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'cache'))

    class H:
        def requirements(self, fam, suite, variant): return [Requirement('artifact', 'x')]
        def resolve(self, req):
            subprocess.run(['true'], env=dict(os.environ, TESTKIT_SECRET=SECRET), check=True, cwd=tmp_path / 'missing')
    item = SimpleNamespace(harness=H(), fam=None, suite=None, variant=None, nodeid='n')
    config = SimpleNamespace(pluginmanager=SimpleNamespace(get_plugin=lambda n: None), rootpath=tmp_path)
    monkeypatch.setattr('testkit.runloop.run_dir', lambda c: tmp_path / 'run')
    failed = prepare.prepare([item], SimpleNamespace(), config)
    cat, why = failed['n']
    assert cat == 'build-failure' and 'FileNotFoundError' in why and SECRET not in why, why


def test_a_harness_error_is_reported_without_frame_arguments(pytester):
    """A harness exception raised under a frame that holds the environment (subprocess.run(..., env=...)) is reported
    without each frame's arguments, so the environment stays out of the report and the JUnit file. (The secret comes
    first: a frame argument's repr is cut in the middle.)"""
    d = pytester.path / 'tests' / 'leak'; d.mkdir(parents=True)
    (d / 'family.toml').write_text('[family]\nname = "leak"\n[[suite]]\nname = "s"\nkind = "judged"\n'
                                   'harness = "harness.py:H"\n')
    (d / 'harness.py').write_text(
        'import os, subprocess\n'
        'class H:\n'
        '    def cases(self, fam, suite): return ["c"]\n'
        '    def run(self, batch, config):\n'
        f'        subprocess.run(["/no/such/tool"], env={{"TESTKIT_SECRET": "{SECRET[:4]}" + "{SECRET[4:]}", **os.environ}})\n')
    r = pytester.runpytest_subprocess('-p', 'testkit.plugin', '--testkit-inprocess', f'--junitxml={pytester.path / "j.xml"}',
                                      'tests')
    out = r.stdout.str() + r.stderr.str()
    assert r.ret == 1 and 'FileNotFoundError' in out, out
    assert SECRET not in out and SECRET not in (pytester.path / 'j.xml').read_text()
