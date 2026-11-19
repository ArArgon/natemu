import hashlib
import http.server
import json
import os
import struct
import subprocess
import threading

import pytest

from testkit import cache, inputs, resources
from testkit.prepare import BuildFailure


@pytest.fixture
def server():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'not the pinned file\n')
        def log_message(self, *a): pass
    s = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{s.server_address[1]}'
    s.shutdown()


def test_wrong_pin_is_unpreparable(server, monkeypatch, tmp_path):
    """A download whose bytes do not match its pin stops the root with Unpreparable, and nothing is published."""
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    real = resources.declarations()
    monkeypatch.setattr(resources, 'declarations', lambda: {n: dict(d, urls=[f'{server}/{n}']) for n, d in real.items()})
    with pytest.raises(resources.Unpreparable) as e:
        inputs.root()
    assert 'pinned' in str(e.value)
    assert not (tmp_path / 'c' / 'input-root').exists()


def test_every_component_is_declared():
    names = set(resources.declarations())
    assert {'debian-netboot-linux', 'debian-netboot-initrd', 'busybox-static-deb', 'debian-kernel-deb', 'qemu-user-static'} <= names


@pytest.mark.slow
def test_root_builds_once_and_holds_exactly_the_inputs():
    r = inputs.root()
    with cache.peek():          # the second build finds every component and the root itself: anything rebuilt raises Miss
        assert inputs.root() == r
    for rel in inputs.PATHS:
        assert (r / rel).exists(), rel
    stray = [p for p in r.rglob('*') if p.is_file() and not any(p.relative_to(r).as_posix().startswith(rel) for rel in inputs.PATHS)]
    assert not stray
    assert not list(r.rglob('expected.json'))           # answers come from the locks, never from the input root


# ---------------------------------------------------------------- container-built inputs (stubbed builds)
def _static_elf() -> bytes:
    """An x86-64 ELF header with one PT_LOAD and no PT_INTERP/PT_DYNAMIC (what the stress recipe checks)."""
    hdr = b'\x7fELF\x02\x01\x01' + b'\0' * 9 + struct.pack('<HHIQQQIHHHHHH', 2, 62, 1, 0, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    return hdr + struct.pack('<IIQQQQQQ', 1, 5, 0, 0, 0, 120, 120, 0x1000)


def test_container_inputs_need_container(monkeypatch, tmp_path):
    """Without Apple's container the recipes stop with a BuildFailure that says what to install."""
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    monkeypatch.setattr(inputs.shutil, 'which', lambda n: None)
    for build in (inputs.fio_prep, inputs.stress_ng):
        with pytest.raises(BuildFailure) as e:
            build()
        assert 'brew install container' in str(e.value)


def test_unreachable_snapshot_is_unpreparable_with_the_url(monkeypatch, tmp_path):
    monkeypatch.setattr(inputs.shutil, 'which', lambda n: '/usr/bin/true')
    url = 'http://snapshot.debian.org/archive/debian/20261108T000000Z/dists/trixie/InRelease'
    monkeypatch.setattr(inputs.subprocess, 'run',
                        lambda *a, **k: subprocess.CompletedProcess(a, 100, '', f'E: Failed to fetch {url}  Could not resolve host\n'))
    with pytest.raises(resources.Unpreparable) as e:
        inputs._in_container('fio-prep', tmp_path, tmp_path)
    assert url in str(e.value)


def test_container_build_timeout_is_a_build_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(inputs.shutil, 'which', lambda n: '/usr/bin/true')
    calls = []
    def run(*a, **k):
        calls.append(a[0][:2])
        if a[0][:2] == ['container', 'run']: raise subprocess.TimeoutExpired(a[0], k['timeout'])
    monkeypatch.setattr(inputs.subprocess, 'run', run)
    with pytest.raises(BuildFailure, match='more than 3600 s'):
        inputs._in_container('stress-ng', tmp_path, tmp_path)
    assert ['container', 'kill'] in calls                 # the container is stopped, not only its client


def test_stress_ng_entry_has_binary_source_and_provenance(monkeypatch, tmp_path):
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    seen = []
    def fake(name, recipe, work, **kw):
        seen.append(recipe); (work / 'bin').mkdir(); (work / 'bin/stress-ng').write_bytes(_static_elf())
        (work / 'source.tar.xz').write_bytes(b'orig')
    monkeypatch.setattr(inputs, '_in_container', fake)
    monkeypatch.setattr(inputs, 'STRESS_BUILD_SHA', hashlib.sha256(_static_elf()).hexdigest())   # the fake build's
    d = inputs.stress_ng()
    prov = json.loads((d / 'provenance.json').read_text())
    assert set(prov) == {'binary_sha256', 'source_sha256', 'recipe_sha256', 'builder'}
    assert prov['binary_sha256'] == hashlib.sha256(_static_elf()).hexdigest()
    assert prov['source_sha256'] == hashlib.sha256(b'orig').hexdigest()
    assert seen[0].name == 'stress-ng' and prov['recipe_sha256'] == inputs.recipe_sha256(seen[0])
    assert inputs.artifacts.BENCH_BUILDER in prov['builder']


def test_dynamic_stress_binary_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    dynamic = bytearray(_static_elf()); struct.pack_into('<I', dynamic, 64, 3)          # PT_INTERP
    def fake(name, recipe, work, **kw):
        (work / 'bin').mkdir(); (work / 'bin/stress-ng').write_bytes(bytes(dynamic)); (work / 'source.tar.xz').write_bytes(b'orig')
    monkeypatch.setattr(inputs, '_in_container', fake)
    with pytest.raises(BuildFailure, match='not fully static'):
        inputs.stress_ng()


def test_fio_prep_runs_the_closure_on_the_container_output(monkeypatch, tmp_path):
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    calls = []
    monkeypatch.setattr(inputs, '_in_container', lambda name, recipe, work, **kw: (work / 'Packages.trixie.xz').write_bytes(b'x'))
    def runner(script, *args):
        calls.append((str(script), args)); args[2].mkdir(); (args[2] / 'elf-closure.json').write_text('{}')
    monkeypatch.setattr(inputs, '_runner', runner)
    d = inputs.fio_prep()
    assert d.name == 'fio-prep' and (d / 'elf-closure.json').exists() and not (d.parent / 'work').exists()
    assert calls[0][0] == 'io/fio-prep/closure.py' and calls[0][1][1].name == 'fio-prep'


def test_writable_clone_restores_the_pinned_modes(tmp_path):
    """Cache entries are read-only; the matrix recipe hands run_io_matrix.py clones whose rootfs modes equal the pin."""
    src = tmp_path / 'src'; (src / 'rootfs/sbin').mkdir(parents=True)
    (src / 'rootfs/sbin/init').write_text('#!/bin/sh\n'); (src / 'rootfs/data').write_text('x')
    (src / 'inputs.json').write_text(json.dumps({'rootfs': {'sbin/init': {'mode': '0o100755'}, 'data': {'mode': '0o100640'}}}))
    (src / 'suid').write_text('x')
    for p in (src / 'rootfs/sbin/init', src / 'rootfs/data', src / 'inputs.json'): p.chmod(0o444)
    (src / 'suid').chmod(0o4555)                                                # a read-only setuid file (chfn in fio's root)
    inputs.writable_clone(src, tmp_path / 'dest')
    mode = lambda rel: (tmp_path / 'dest' / rel).stat().st_mode & 0o7777
    assert (mode('rootfs/sbin/init'), mode('rootfs/data'), mode('inputs.json'), mode('suid')) == (0o755, 0o640, 0o644, 0o4755)
    assert (src / 'rootfs/sbin/init').stat().st_mode & 0o777 == 0o444        # the source is untouched


def test_matrix_inputs_prepares_from_writable_clones(monkeypatch, tmp_path):
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    fio, block = tmp_path / 'fio', tmp_path / 'block'
    for d in (fio, block): d.mkdir(); (d / 'f').write_text('x'); (d / 'f').chmod(0o444)
    monkeypatch.setattr(inputs, 'fio_prep', lambda: fio); monkeypatch.setattr(inputs, 'block_inputs', lambda: block)
    calls = []
    def runner(script, *args):
        assert args[0] == 'prepare'
        a = dict(zip(args[1::2], args[2::2])); calls.append((script, a))
        assert all(os.access(a[k] / 'f', os.W_OK) for k in ('--fio-prep', '--block-inputs'))
        a['--out'].mkdir(); (a['--out'] / 'inputs.json').write_text('{}')
    monkeypatch.setattr(inputs, '_runner', runner)
    d = inputs.matrix_inputs()
    assert calls[0][0] == 'run_io_matrix.py' and d.name == 'inputs' and (d / 'inputs.json').exists()
    assert not (d.parent / 'work').exists()


def test_new_paths_are_components(monkeypatch, tmp_path):
    stub = {n: tmp_path / n for n in ('fio_prep', 'stress_ng', 'block_inputs', 'network_rootfs', 'qemu_user', 'corpus', 'fetch')}
    for n in ('fio_prep', 'stress_ng', 'block_inputs', 'network_rootfs'): monkeypatch.setattr(inputs, n, lambda n=n: stub[n])
    monkeypatch.setattr(inputs, 'corpus', lambda g: stub['corpus'])
    monkeypatch.setattr(inputs.artifacts, 'qemu_user', lambda: stub['qemu_user'])
    monkeypatch.setattr(inputs.resources, 'fetch', lambda n: stub['fetch'])
    parts = inputs.components()
    assert set(parts) == set(inputs.PATHS)
    assert parts[inputs.FIO_PREP] == stub['fio_prep'] and parts[inputs.STRESS] == stub['stress_ng']


def test_stress_build_pin_is_the_same_everywhere():
    import importlib.util, sys
    from testkit import inputs, paths
    sys.path.insert(0, str(paths.repo_root() / 'tools'))
    pins = set()
    for name in ('run_io_soak', 'run_serial_stress'):
        src = (paths.repo_root() / 'tools' / f'{name}.py').read_text()
        pins |= {line.split('=', 1)[1].split('#')[0].strip().strip("'") for line in src.splitlines() if line.startswith('BUILD_SHA')}
    assert pins == {inputs.STRESS_BUILD_SHA}


def test_stress_build_that_stops_reproducing_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    def fake(name, recipe, work, **kw):
        (work / 'bin').mkdir(); (work / 'bin/stress-ng').write_bytes(_static_elf()); (work / 'source.tar.xz').write_bytes(b'orig')
    monkeypatch.setattr(inputs, '_in_container', fake)
    with pytest.raises(BuildFailure, match='no longer reproduces'):
        inputs.stress_ng()
