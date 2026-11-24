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


def test_platform_inputs_runs_the_devices_prepare(monkeypatch, tmp_path):
    """tests/devices/run_platform.py prepare, from the netboot kernel, its package and the BusyBox package."""
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    files = {n: tmp_path / n for n in ('debian-netboot-linux', 'debian-kernel-deb', 'busybox-static-deb')}
    for f in files.values(): f.write_text(f.name)
    monkeypatch.setattr(inputs.resources, 'fetch', lambda n: files[n])
    calls = []
    def runner(script, *args):
        a = dict(zip(args[1::2], args[2::2])); calls.append((script, args[0], a))
        a['--out'].mkdir(); (a['--out'] / 'inputs.json').write_text('{}')
    monkeypatch.setattr(inputs, '_runner', runner)
    d = inputs.platform_inputs()
    script, command, a = calls[0]
    assert script.as_posix().endswith('tests/devices/run_platform.py') and command == 'prepare'
    assert {k: v for k, v in a.items() if k != '--out'} == {'--kernel': str(files['debian-netboot-linux']),
                                                           '--kernel-deb': str(files['debian-kernel-deb']),
                                                           '--busybox-deb': str(files['busybox-static-deb'])}
    assert d.name == 'platform' and (d / 'inputs.json').exists()


def test_new_paths_are_components(monkeypatch, tmp_path):
    stub = {n: tmp_path / n for n in ('fio_prep', 'stress_ng', 'block_inputs', 'network_rootfs', 'qemu_user', 'corpus', 'fetch',
                                      'matrix_inputs', 'platform_inputs')}
    for n in ('fio_prep', 'stress_ng', 'block_inputs', 'network_rootfs', 'matrix_inputs', 'platform_inputs'):
        monkeypatch.setattr(inputs, n, lambda n=n: stub[n])
    monkeypatch.setattr(inputs, 'corpus', lambda g: stub['corpus'])
    monkeypatch.setattr(inputs.artifacts, 'qemu_user', lambda: stub['qemu_user'])
    monkeypatch.setattr(inputs.resources, 'fetch', lambda n: stub['fetch'])
    parts = inputs.components()
    assert set(parts) == set(inputs.PATHS)
    assert parts[inputs.FIO_PREP] == stub['fio_prep'] and parts[inputs.STRESS] == stub['stress_ng']
    assert parts[inputs.MATRIX] == stub['matrix_inputs'] and parts[inputs.PLATFORM] == stub['platform_inputs']


# ---------------------------------------------------------------- IO5 inputs (stubbed prepare)
def test_io5_inputs_prepares_from_the_pinned_downloads(monkeypatch, tmp_path):
    """run_io5.py prepare runs on a folder that already holds the two pinned downloads (so it downloads nothing), and
    the entry keeps what `run` reads, not the image tarball."""
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    dl = tmp_path / 'dl'; dl.mkdir()
    files = {'debian-cloud-nocloud': dl / 'debian-13-nocloud-amd64-20260914-2601.tar.xz', 'cpython-src': dl / 'Python-3.13.18.tar.xz'}
    for f in files.values(): f.write_text(f.name); f.chmod(0o444)
    monkeypatch.setattr(inputs.resources, 'fetch', lambda n: files[n])
    monkeypatch.setattr(inputs, 'e2fsprogs', lambda: (tmp_path / 'e2fs', '1.47.3'))
    calls = []
    def runner(script, *args):
        calls.append((script, args))
        d = args[list(args).index('--inputs') + 1]
        assert {p.name for p in d.iterdir()} == {f.name for f in files.values()}       # nothing left to download
        for n in ('disk.raw', 'vmlinuz', 'initrd.img', 'inputs.json'): (d / n).write_text(n)
    monkeypatch.setattr(inputs, '_runner', runner)
    d = inputs.io5_inputs()
    assert calls[0][0] == 'run_io5.py' and calls[0][1][0] == 'prepare'
    assert {p.name for p in d.iterdir()} == {'disk.raw', 'vmlinuz', 'initrd.img', 'inputs.json', 'Python-3.13.18.tar.xz'}


def test_io5_downloads_are_the_runners_pins():
    """The resources io5_inputs fetches are the files run_io5.py names and pins (prepare checks the image's sha512)."""
    import re
    from testkit import paths
    src = (paths.repo_root() / 'tests' / 'io' / 'run_io5.py').read_text()
    val = lambda name: re.search(rf"^{name} = '([^']+)'", src, re.M)[1]
    decl = resources.declarations()
    assert decl['debian-cloud-nocloud']['urls'][0].endswith('/' + val('IMAGE') + '.tar.xz')
    assert decl['cpython-src']['urls'][0].endswith('/' + val('PYTHON'))
    assert decl['cpython-src']['sha256'] == val('PYTHON_SHA')


def test_container_builds_run_natively_on_the_x86_builder(monkeypatch, tmp_path):
    """[build] builder = "ssh:host": the same pinned image under Docker there; recipe and work folder copied in, the
    work folder copied back, the temporary folder removed through the container (its files are root's)."""
    import subprocess as sp
    from types import SimpleNamespace
    monkeypatch.setenv('NATEMU_CONTAINER_BUILDER', 'ssh:u@x86')
    recipe = tmp_path / 'recipe'; recipe.mkdir(); (recipe / 'container.sh').write_text('true')
    work = tmp_path / 'work'; work.mkdir()
    seen = []; tar_envs = []
    def run(cmd, **kw):
        seen.append(cmd)
        if cmd[:1] == ['tar'] and '-cf' in cmd: tar_envs.append(kw.get('env') or {})
        if cmd[:1] == ['tar'] and '-cf' in cmd: return SimpleNamespace(returncode=0, stdout=b'TAR', stderr=b'')
        if cmd[:1] == ['tar']: return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        last = cmd[-1]
        if last.startswith('mktemp'): return SimpleNamespace(returncode=0, stdout='/home/u/.natemu-build-1\n', stderr='')
        if last.startswith('tar -C /home/u/.natemu-build-1/work '): return SimpleNamespace(returncode=0, stdout=b'BACK', stderr=b'')
        return SimpleNamespace(returncode=0, stdout='' if kw.get('text') else b'', stderr='' if kw.get('text') else b'')
    monkeypatch.setattr(inputs.subprocess, 'run', run)
    monkeypatch.setattr(inputs.artifacts, '_run', lambda cmd, **kw: run(cmd, text=True, **kw))
    inputs._in_container('t', recipe, work)
    remote = [c[-1] for c in seen if c[:1] == ['ssh']]
    assert any('--cpus $(n=$(nproc); echo $((n < 8 ? n : 8)))' in r for r in remote)      # fits the machine, 8 at most
    assert any(r.startswith(inputs._slot_wait() + 'docker run --rm') and inputs.artifacts.BENCH_BUILDER in r and
               '-v /home/u/.natemu-build-1/recipe:/recipe:ro' in r and 'bash /recipe/container.sh' in r for r in remote)
    assert 'flock -n 9' in inputs._slot_wait() and 'nproc' in inputs.BUILD_SLOTS   # one slot per 4 CPUs there
    assert any("tar -C /home/u/.natemu-build-1/work --exclude='._*' -cf -" in r for r in remote)  # the work folder back
    # no AppleDouble ._NAME files either way: the Mac's tar runs with COPYFILE_DISABLE, the builder's excludes them
    assert tar_envs and all(e.get('COPYFILE_DISABLE') == '1' for e in tar_envs)
    assert ['tar', '-C', str(work), '-xf', '-'] in seen
    assert remote[-1].startswith('docker run --rm -v /home/u/.natemu-build-1:/x') and 'rm -rf' in remote[-1]


def test_builder_comes_from_the_machine_config_and_keys_the_entry(monkeypatch, tmp_path):
    monkeypatch.delenv('NATEMU_CONTAINER_BUILDER', raising=False)
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'cache'))
    monkeypatch.setattr(inputs.artifacts.paths, 'repo_root', lambda: tmp_path / 'wt')
    (tmp_path / 'cache').mkdir()
    assert inputs.artifacts.container_builder() == 'local'
    local = inputs.artifacts.build_env()
    (tmp_path / 'cache' / 'config.toml').write_text('[build]\nbuilder = "ssh:u@x86"\n')
    assert inputs.artifacts.container_builder() == 'ssh:u@x86'
    assert inputs.artifacts.build_env() != local and inputs.artifacts.build_env() == inputs.artifacts.BENCH_BUILDER + ' native'
    (tmp_path / 'wt' / '.natemu').mkdir(parents=True)
    (tmp_path / 'wt' / '.natemu' / 'config.toml').write_text('[build]\nbuilder = "local"\n')   # the worktree wins
    assert inputs.artifacts.container_builder() == 'local'





def _facts(kernel='7.0.0-43-generic', model='AMD Ryzen 7 8845HS w/ Radeon 780M Graphics', machine_id='0123abcd', vendor='AuthenticAMD'):
    return f'kernel: {kernel}\nmachine-id: {machine_id}\nvendor_id\t: {vendor}\nmodel name\t: {model}\n'


def test_native_builders_are_keyed_by_platform_not_host_and_provenance_records_the_host(monkeypatch, tmp_path):
    """A container-built input is keyed by its inputs: the image and the platform. Two x86 machines with another
    address, CPU and kernel give the same key, so moving the builder keeps the cache; this Mac (Rosetta) gives another.
    Provenance records the facts: address, machine-id (when there is one), kernel and CPU, read once per process."""
    from types import SimpleNamespace
    a = inputs.artifacts
    monkeypatch.setattr(a, '_builder_facts_read', {})
    hosts = {'u@a': _facts(), 'u@b': _facts(kernel='7.0.0-44-generic', model='Intel(R) Xeon(R) w5-2455X', machine_id='4567',
                       vendor='GenuineIntel'),
             'u@noid': _facts(machine_id='')}
    calls = []
    def run(cmd, **kw):
        calls.append(cmd[-2])
        return SimpleNamespace(stdout=hosts[cmd[-2]])
    monkeypatch.setattr(a, '_run', run)
    def key(b):
        monkeypatch.setenv('NATEMU_CONTAINER_BUILDER', b)
        return a.build_env()
    k = key('ssh:u@a')
    assert k == key('ssh:u@b') == a.BENCH_BUILDER + ' native' and not calls        # the key never reads the host
    assert key('local') == a.BENCH_BUILDER != k
    def described(b):
        monkeypatch.setenv('NATEMU_CONTAINER_BUILDER', b)
        return a.build_env_described()
    assert described('ssh:u@b') == (a.BENCH_BUILDER + ' under Docker on u@b (native): machine-id 4567, '
                                    'kernel 7.0.0-44-generic, CPU GenuineIntel Intel(R) Xeon(R) w5-2455X')
    assert described('ssh:u@noid').endswith('(native): kernel 7.0.0-43-generic, CPU AuthenticAMD AMD Ryzen 7 8845HS w/ Radeon 780M Graphics')
    described('ssh:u@b'); assert calls.count('u@b') == 1                           # once per process
    assert described('local').startswith('local: ')
    monkeypatch.setattr(a, '_run', lambda cmd, **kw: SimpleNamespace(stdout='kernel: 7.0\n'))
    with pytest.raises(a.BuildFailure): a._builder_facts('u@odd')                  # not facts: refused


def test_a_run_uses_the_inputs_it_prepared_even_if_the_builder_changes(monkeypatch, tmp_path):
    """The torture part of the first campaign run rebuilt its image at run time: the machine's [build] builder changed
    while the run went on, so the worker's key differed from the one prepared. In a run, derived inputs are pinned."""
    from testkit import resources
    monkeypatch.setenv('NATEMU_TEST_CACHE', str(tmp_path / 'c'))
    monkeypatch.setenv('TESTKIT_RUN_DIR', str(tmp_path / 'run'))
    monkeypatch.setenv('TESTKIT_ARTIFACTS', '{}')
    built = []
    def recipe(out, inputs): built.append(inputs[1]); (out / 'f').write_text(inputs[1])
    a = resources.derived('thing', recipe, ['x', 'builder-1'])
    b = resources.derived('thing', recipe, ['x', 'builder-2'])        # the config changed mid-run
    assert a == b and built == ['builder-1']
