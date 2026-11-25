"""The input root: one cache entry laid out like natemu-raw that holds exactly the inputs the old runners (tools/run_*.py)
read, each built on this machine from a recipe in the repo or a pinned download (spec R14), never copied. Run an old
runner against it with NATEMU_RAW_ROOT=<root()> and NATEMU_STATE_ROOT=<a writable folder>: the entry is read-only and
the runners' locks and caches go to the state root (tools/raw_root.py). Answers (expected.json) are not inputs: they
come from the locks."""
import hashlib
import inspect
import json
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import time

from testkit import artifacts, cache, paths, resources
from testkit.prepare import BuildFailure

KERNEL_DEB = 'linux-image-6.12.107+deb13-amd64_6.12.107-1_amd64.deb'
# Where each input sits in the root (the old runners' paths under NATEMU_RAW_ROOT).
NETBOOT_LINUX = 'kernel-census-20261028-01/dl/linux'
NETBOOT_INITRD = 'kernel-census-20261028-01/dl/initrd.gz'
QEMU_USER = 'reference-slice1-20261026-01/pin/qemu-x86_64-static'
BLOCK_INPUTS = 'io3-block-20261108-01/inputs-01'
BLOCK_KERNEL_DEB = f'io3-block-20261108-01/prerequisites/{KERNEL_DEB}'
NETWORK_ROOTFS = 'io-network-tools-20261108-01/rootfs'
I7_PROGRAMS = 'i7-corpus-20261103-01/programs.json'
S2D_PROGRAMS = 's2d/corpus/programs.json'
FIO_PREP = 'io-benchmark-prep-20261108-01'                  # run_io_matrix.py / run_fio_admission.py --fio-prep
STRESS = 'io-safety-stress-20261108-01'                     # bin/stress-ng, source.tar.xz, provenance.json
MATRIX = 'io-matrix-inputs-20261118-01'                     # run_io_matrix.py prepare's folder (the serial-stress and soak campaigns)
PLATFORM = 'devices-platform/platform'                      # tests/devices/run_platform.py prepare's folder (its run --inputs)
CONSOLE = 'devices-console/console'                         # tests/devices/run_console.py prepare's folder (its run --inputs)
CONTROL = 'devices-control/control'                         # tests/devices/run_control.py prepare's folder (its run --inputs)
DISPLAY = 'devices-display/display'                         # tests/devices/run_display.py prepare's folder (its run --inputs)
PATHS = (NETBOOT_LINUX, NETBOOT_INITRD, QEMU_USER, BLOCK_INPUTS, BLOCK_KERNEL_DEB, NETWORK_ROOTFS, I7_PROGRAMS, S2D_PROGRAMS,
         FIO_PREP, STRESS, MATRIX, PLATFORM, CONSOLE, CONTROL, DISPLAY)
E2FSPROGS = pathlib.Path('/opt/homebrew/opt/e2fsprogs')     # Homebrew's: a host tool, checked (version and hashes key the image), not pinned


def _tools() -> pathlib.Path:
    return paths.repo_root() / 'tools'


def _io() -> pathlib.Path:
    """The io family's scripts (their `prepare` commands build these inputs) and tests/lib, which they import."""
    return paths.repo_root() / 'tests' / 'io'


def _lib() -> pathlib.Path:
    return paths.repo_root() / 'tests' / 'lib'


def _runner(script, *args) -> None:
    """Run a build step's script: a path, or a name in tests/io (the io scripts' `prepare` commands), else under tools/."""
    path = script if pathlib.Path(script).is_absolute() else _io() / script if (_io() / script).is_file() else _tools() / script
    artifacts._run([sys.executable, '-B', path, *map(str, args)], cwd=paths.repo_root())


def _clang() -> str:
    """The host assembler the corpora and the block image's programs are assembled with (part of their keys)."""
    try:
        return subprocess.run(['clang', '--version'], capture_output=True, text=True, check=True).stdout.splitlines()[0]
    except (OSError, subprocess.CalledProcessError) as e:
        raise BuildFailure('clang', f'host clang is needed to assemble x86 programs ({e})')


def _rng_recipe(out: pathlib.Path, inputs: list) -> None:
    kernel, initrd, busybox = inputs[:3]
    _runner('run_virtio_rng.py', 'prepare', '--kernel', kernel, '--installer', initrd, '--busybox-deb', busybox, '--out', out / 'rng')


def rng_inputs() -> pathlib.Path:
    """tests/io/run_virtio_rng.py prepare's folder: the netboot kernel, stock modules from the netboot initrd, Debian's BusyBox."""
    t, io, lib = _tools(), _io(), _lib()
    srcs = [str(resources.fetch(n)) for n in ('debian-netboot-linux', 'debian-netboot-initrd', 'busybox-static-deb')]
    return resources.derived('io-rng-inputs', _rng_recipe, srcs + [io / 'run_virtio_rng.py', lib / 'io_common.py', t / 'play/mkcpio.py']) / 'rng'


def _platform_recipe(out: pathlib.Path, inputs: list) -> None:
    kernel, kernel_deb, busybox = inputs[:3]
    _runner(paths.repo_root() / 'tests' / 'devices' / 'run_platform.py', 'prepare', '--kernel', kernel, '--kernel-deb', kernel_deb,
            '--busybox-deb', busybox, '--out', out / 'platform')


def platform_inputs() -> pathlib.Path:
    """tests/devices/run_platform.py prepare's folder: the netboot kernel, Debian's BusyBox, and the stock modules the
    devices family's cases load (with their dependencies) from the same kernel's package, in platform.cpio."""
    t, lib = _tools(), _lib()
    srcs = [str(resources.fetch(n)) for n in ('debian-netboot-linux', 'debian-kernel-deb', 'busybox-static-deb')]
    return resources.derived('devices-platform-inputs', _platform_recipe,
                             srcs + [paths.repo_root() / 'tests' / 'devices' / 'run_platform.py', lib / 'io_common.py',
                                     t / 'raw_root.py', t / 'play/mkcpio.py']) / 'platform'


def _console_recipe(out: pathlib.Path, inputs: list) -> None:
    kernel, kernel_deb, busybox = inputs[:3]
    _runner(paths.repo_root() / 'tests' / 'devices' / 'run_console.py', 'prepare', '--kernel', kernel, '--kernel-deb', kernel_deb,
            '--busybox-deb', busybox, '--out', out / 'console')


def console_inputs() -> pathlib.Path:
    """tests/devices/run_console.py prepare's folder: the netboot kernel, Debian's BusyBox, the stock virtio_mmio module
    from the same kernel's package and the pattern the console-file case writes, in console.cpio."""
    t, lib, dev = _tools(), _lib(), paths.repo_root() / 'tests' / 'devices'
    srcs = [str(resources.fetch(n)) for n in ('debian-netboot-linux', 'debian-kernel-deb', 'busybox-static-deb')]
    return resources.derived('devices-console-inputs', _console_recipe,
                             srcs + [dev / 'run_console.py', dev / 'run_platform.py', lib / 'io_common.py',
                                     t / 'raw_root.py', t / 'play/mkcpio.py']) / 'console'


def _control_recipe(out: pathlib.Path, inputs: list) -> None:
    kernel, kernel_deb, busybox = inputs[:3]
    _runner(paths.repo_root() / 'tests' / 'devices' / 'run_control.py', 'prepare', '--kernel', kernel, '--kernel-deb', kernel_deb,
            '--busybox-deb', busybox, '--out', out / 'control')


def control_inputs() -> pathlib.Path:
    """tests/devices/run_control.py prepare's folder: the netboot kernel, Debian's BusyBox and the stock evdev, psmouse
    and button modules from the same kernel's package, in control.cpio."""
    t, lib, dev = _tools(), _lib(), paths.repo_root() / 'tests' / 'devices'
    srcs = [str(resources.fetch(n)) for n in ('debian-netboot-linux', 'debian-kernel-deb', 'busybox-static-deb')]
    return resources.derived('devices-control-inputs', _control_recipe,
                             srcs + [dev / 'run_control.py', dev / 'run_platform.py', lib / 'io_common.py',
                                     t / 'raw_root.py', t / 'play/mkcpio.py']) / 'control'


def _kms_recipe(out: pathlib.Path, inputs: list) -> None:
    source, script, pins = map(pathlib.Path, inputs[:3])
    recipe = out / 'recipe'; recipe.mkdir()          # what the container's /recipe holds: exactly the keyed files
    for f in (source, script, pins / 'packages.txt', pins / 'debs.sha256'): shutil.copy2(f, recipe / f.name)
    work = out / 'work'; work.mkdir()
    _in_container('kms-pattern', recipe, work, script=script.name, pins_to=pins)
    b = (work / 'kms_pattern').read_bytes()
    if b[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', b, 18)[0] != 62: raise BuildFailure('kms-pattern', 'not an x86-64 ELF')
    ph, size, count = struct.unpack_from('<Q', b, 32)[0], *struct.unpack_from('<HH', b, 54)
    if any(struct.unpack_from('<I', b, ph + i * size)[0] in (2, 3) for i in range(count)): raise BuildFailure('kms-pattern', 'not fully static')
    for n in ('kms_pattern', 'build.log'): shutil.move(work / n, out / n)
    (out / 'provenance.json').write_text(json.dumps({
        'binary_sha256': hashlib.sha256(b).hexdigest(), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'builder': artifacts.build_env_described()}, indent=1) + '\n')
    shutil.rmtree(recipe); shutil.rmtree(work)


def kms_pattern() -> pathlib.Path:
    """tests/devices/kms_pattern.c built static for x86-64 (tests/devices/kms_pattern.sh) in the amd64 Debian builder,
    on the stress-ng recipe's pinned sysroot (tools/io/stress-ng: gcc 14, glibc, the guest kernel's UAPI headers): the
    folder holding kms_pattern, build.log and provenance.json."""
    dev = paths.repo_root() / 'tests' / 'devices'
    return resources.derived('kms-pattern', _kms_recipe, [dev / 'kms_pattern.c', dev / 'kms_pattern.sh',
                                                          _tools() / 'io' / 'stress-ng', artifacts.build_env()])


def _display_recipe(out: pathlib.Path, inputs: list) -> None:
    kms, kernel, kernel_deb, busybox = inputs[:4]
    _runner(paths.repo_root() / 'tests' / 'devices' / 'run_display.py', 'prepare', '--kernel', kernel, '--kernel-deb', kernel_deb,
            '--busybox-deb', busybox, '--kms-pattern', pathlib.Path(kms) / 'kms_pattern', '--out', out / 'display')


def display_inputs() -> pathlib.Path:
    """tests/devices/run_display.py prepare's folder: the netboot kernel, Debian's BusyBox, the bochs DRM module and what
    it depends on from the same kernel's package, and kms_pattern(), in display.cpio."""
    t, lib, dev = _tools(), _lib(), paths.repo_root() / 'tests' / 'devices'
    srcs = [str(kms_pattern())] + [str(resources.fetch(n)) for n in ('debian-netboot-linux', 'debian-kernel-deb', 'busybox-static-deb')]
    return resources.derived('devices-display-inputs', _display_recipe,
                             srcs + [dev / 'run_display.py', dev / 'run_platform.py', lib / 'io_common.py',
                                     t / 'raw_root.py', t / 'play/mkcpio.py']) / 'display'



def e2fsprogs() -> tuple[pathlib.Path, str]:
    """Homebrew's e2fsprogs (its Cellar folder, as tests/io/run_virtio_block.py records it) and its version."""
    prefix = pathlib.Path(os.environ.get('NATEMU_E2FSPROGS') or E2FSPROGS).resolve()
    if not all((prefix / 'sbin' / n).is_file() for n in ('mke2fs', 'e2fsck', 'debugfs')):
        raise BuildFailure('e2fsprogs', f'no mke2fs, e2fsck and debugfs under {prefix}/sbin: brew install e2fsprogs')
    return prefix, prefix.name


def _block_recipe(out: pathlib.Path, inputs: list) -> None:
    rng, kernel_deb, e2fs = inputs[:3]
    _runner('run_virtio_block.py', 'prepare', '--rng-inputs', rng, '--kernel-deb', kernel_deb, '--e2fsprogs', e2fs, '--out', out / 'inputs-01')


def block_inputs() -> pathlib.Path:
    """tests/io/run_virtio_block.py prepare's folder (mmio): the ext4 root image and its boot initramfs."""
    t, io, lib = _tools(), _io(), _lib()
    prefix, version = e2fsprogs()
    conf = prefix.parent.parent / 'etc/mke2fs.conf'         # the configuration prepare copies (its own fallback order)
    conf = conf if conf.exists() else pathlib.Path('/opt/homebrew/etc/mke2fs.conf')
    host = [prefix / 'sbin' / n for n in ('mke2fs', 'e2fsck', 'debugfs')] + ([conf] if conf.exists() else [])
    srcs = [str(rng_inputs()), str(resources.fetch('debian-kernel-deb')), str(prefix), version, _clang()]
    return resources.derived('io-block-inputs', _block_recipe,
                             srcs + host + [io / 'run_virtio_block.py', io / 'run_virtio_rng.py', lib / 'io_common.py',
                                            t / 'play/mkcpio.py', t / 'reference/ref.py']) / 'inputs-01'


def _network_recipe(out: pathlib.Path, inputs: list) -> None:
    recipe = pathlib.Path(inputs[0])
    work = out / 'work'; work.mkdir()
    _in_container('network-rootfs', recipe, work)
    (out / 'rootfs').mkdir()
    artifacts._run(['tar', '-C', out / 'rootfs', '-xpf', work / 'rootfs.tar'])
    for n in ('packages.txt', 'debs.sha256'): shutil.copy2(work / n, out / n)
    shutil.rmtree(work)


def network_rootfs() -> pathlib.Path:
    """The iperf3/curl/openssl tree tests/io/run_net_linux.py copies into its guest: tools/io/network-rootfs, pinned Debian
    packages unpacked in an amd64 Debian container (the bench image's builder)."""
    recipe = _tools() / 'io' / 'network-rootfs'
    return resources.derived('io-network-rootfs', _network_recipe, [recipe, artifacts.build_env()]) / 'rootfs'


def _in_container(name: str, recipe: pathlib.Path, work: pathlib.Path, memory: str = '4g', timeout: int = 3600,
                  script: str = 'container.sh', pins_to: pathlib.Path | None = None) -> None:
    """recipe/<script> in the amd64 Debian builder (artifacts.BENCH_BUILDER), /recipe read-only and /work = work: on
    this Mac through Rosetta, or natively under Docker on the x86 machine artifacts.container_builder() names (the
    recipe is copied there, the work folder copied back, nothing kept there). A download that fails is Unpreparable
    (the URL is in the message), anything else a BuildFailure. New pins a re-pinning build wrote (work's packages.txt,
    debs.sha256, sources.sha256, or a work/repin tree) are kept in the state folder, to be checked and copied into
    `pins_to` (default: the recipe)."""
    builder = artifacts.container_builder()
    cname = f'natemu-build-{name}-{os.getpid()}'
    try:
        if builder == 'local':
            if not shutil.which('container'): raise BuildFailure(name, "needs Apple's `container` (brew install container)")
            cmd = ['container', 'run', '--rm', '--name', cname, '--arch', 'amd64', '--cpus', '4', '--memory', memory,
                   '--volume', f'{recipe}:/recipe:ro', '--volume', f'{work}:/work', artifacts.BENCH_BUILDER, 'bash', f'/recipe/{script}']
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                try: subprocess.run(['container', 'kill', cname], capture_output=True, timeout=60)  # the client's death does not stop it
                except (OSError, subprocess.SubprocessError): pass
                raise
        else:
            r = _in_docker_over_ssh(builder[4:], cname, recipe, work, memory, timeout, script)
    except subprocess.TimeoutExpired:
        raise BuildFailure(name, f'{recipe}/{script} took more than {timeout} s')
    if r.returncode:
        tail = (r.stderr or r.stdout)[-1500:]
        repin = [f for f in ('packages.txt', 'debs.sha256', 'sources.sha256') if (work / f).is_file()]
        tree = work / 'repin'
        repin += sorted(f'repin/{p.relative_to(tree).as_posix()}' for p in tree.rglob('*') if p.is_file()) if tree.is_dir() else []
        if repin:                    # re-pinning: keep the new pins before the failed entry is deleted
            keep = paths.state_dir() / 'repin' / name; keep.mkdir(parents=True, exist_ok=True)
            for f in repin:
                dest = keep / f.removeprefix('repin/'); dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(work / f, dest)
            shown = [f.removeprefix('repin/') for f in repin]
            raise BuildFailure(name, f'no pins in {pins_to or recipe}: new ones written to {keep} ({", ".join(shown)}); check '
                                     f'them, copy them into {pins_to or recipe} and build again')
        if re.search(r'Failed to fetch|Could not resolve|Temporary failure resolving|Unable to connect|Unable to fetch', tail):
            raise resources.Unpreparable(name, tail)
        raise BuildFailure(name, tail)


SSH = ['-o', 'BatchMode=yes', '-o', 'ServerAliveInterval=60']


# builds at once on the x86 machine: one per 4 CPUs (two on the 8-core machine: five at once ran at load 27, all late;
# one on a 4-CPU guest, where two 8g builds would also overcommit its RAM), counted there so any machine fits
BUILD_SLOTS = '$(n=$(nproc); echo $((n / 4 > 1 ? n / 4 : 1)))'


def _slot_wait() -> str:
    """Shell prefix that waits for one of BUILD_SLOTS flock slots on the builder (shared by every Mac using it) and
    holds it, on fd 9, until the build's shell exits."""
    return (f'got=; while [ -z "$got" ]; do for i in $(seq 0 $(({BUILD_SLOTS} - 1))); do exec 9>/tmp/natemu-build-slot-$i; '
            f'if flock -n 9; then got=$i; break; fi; done; [ -n "$got" ] || sleep 5; done; ')


def _in_docker_over_ssh(host: str, cname: str, recipe: pathlib.Path, work: pathlib.Path, memory: str, timeout: int,
                        script: str) -> subprocess.CompletedProcess:
    """_in_container's run on an x86-64 machine: the same pinned image under Docker, natively. The recipe and the work
    folder go there by tar, the work folder comes back the same way (outputs, logs, new pins), and the temporary folder
    there is removed (through the container, as its files are root's)."""
    image = artifacts.BENCH_BUILDER
    d = artifacts._run(['ssh', *SSH, host, 'mktemp -d "$HOME/.natemu-build-XXXXXX"']).stdout.strip()
    try:
        for local, there in ((recipe, 'recipe'), (work, 'work')):
            # COPYFILE_DISABLE: macOS tar would add an AppleDouble `._NAME` file for every file with extended attributes;
            # they come back as plain files that the Mac's tar then fails to unpack ("./._.: Truncated tar archive")
            try:            # a failure is a BuildFailure from None: its traceback would show the environment
                tar = subprocess.run(['tar', '-C', str(local), '-cf', '-', '.'], capture_output=True, check=True,
                                     env={**os.environ, 'COPYFILE_DISABLE': '1'}).stdout
            except (OSError, subprocess.CalledProcessError) as e:
                err = e.stderr.decode(errors='replace')[-800:] if isinstance(e, subprocess.CalledProcessError) else str(e)
                raise BuildFailure(cname, f'packing {local} failed: {err}') from None
            p = subprocess.run(['ssh', *SSH, host, f'mkdir -p {d}/{there} && tar -C {d}/{there} -xf -'], input=tar, capture_output=True)
            if p.returncode: raise BuildFailure(cname, p.stderr.decode(errors='replace')[-800:])
        run = (f'{_slot_wait()}docker run --rm --name {cname} --platform linux/amd64 --cpus $(n=$(nproc); echo $((n < 8 ? n : 8))) --memory {memory} '
               f'-v {d}/recipe:/recipe:ro -v {d}/work:/work {image} bash /recipe/{script}')
        try:
            r = subprocess.run(['ssh', *SSH, host, run], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            subprocess.run(['ssh', *SSH, host, f'docker kill {cname}'], capture_output=True, timeout=60)
            raise
        back = subprocess.run(['ssh', *SSH, host, f"tar -C {d}/work --exclude='._*' -cf - ."], capture_output=True)
        if back.returncode == 0:
            x = subprocess.run(['tar', '-C', str(work), '-xf', '-'], input=back.stdout, capture_output=True)
            if x.returncode:
                raise BuildFailure(cname, 'copying the work folder back failed: ' + x.stderr.decode(errors='replace')[-1500:])
        return r
    finally:
        subprocess.run(['ssh', *SSH, host, f'docker run --rm -v {d}:/x {image} rm -rf /x/recipe /x/work; rmdir {d}'],
                       capture_output=True, timeout=600)


def _fio_recipe(out: pathlib.Path, inputs: list) -> None:
    recipe = pathlib.Path(inputs[0])
    work = out / 'work'; work.mkdir()
    _in_container('fio-prep', recipe, work)
    _runner(recipe.relative_to(_tools()) / 'closure.py', work, recipe, out / 'fio-prep')
    shutil.rmtree(work)


def fio_prep() -> pathlib.Path:
    """The fio preparation (run_io_matrix.py prepare --fio-prep): tools/io/fio-prep, Debian's fio 3.39-1 and its
    dependency closure from pinned snapshot packages, unpacked with its ELF closure and the reviewed profiles."""
    recipe = _tools() / 'io' / 'fio-prep'
    return resources.derived('io-fio-prep', _fio_recipe, [recipe, artifacts.build_env()]) / 'fio-prep'


def writable_clone(src: pathlib.Path, dest: pathlib.Path) -> None:
    """A clone (APFS clonefile, else a copy) of an input folder with the write permission a cache entry takes away
    (cache._readonly) given back: runners that check their inputs' tree manifests (modes included) or rewrite files
    they copied need the modes the folder was built with. Where the folder has an inputs.json with a 'rootfs' manifest
    (run_virtio_block's, run_io_matrix's), its rootfs modes are restored exactly from it. Every mode is taken from the
    source, since a clone drops setuid bits."""
    if subprocess.run(['cp', '-c', '-R', '-p', src, dest], capture_output=True).returncode:
        shutil.rmtree(dest, ignore_errors=True); artifacts._run(['cp', '-R', '-p', src, dest])
    for p in src.rglob('*'):
        if p.is_file() and not p.is_symlink(): (dest / p.relative_to(src)).chmod((p.stat().st_mode & 0o7777) | 0o200)
    pin = dest / 'inputs.json'
    if pin.is_file():
        for rel, e in json.loads(pin.read_text()).get('rootfs', {}).items():
            p = dest / 'rootfs' / rel
            if 'mode' in e and not p.is_symlink() and p.exists(): p.chmod(int(e['mode'], 8) & 0o7777)


def _matrix_recipe(out: pathlib.Path, inputs: list) -> None:
    fio, block = map(pathlib.Path, inputs[:2])
    work = out / 'work'; work.mkdir()
    writable_clone(fio, work / 'fio-prep'); writable_clone(block, work / 'block-inputs')
    _runner('run_io_matrix.py', 'prepare', '--fio-prep', work / 'fio-prep', '--block-inputs', work / 'block-inputs', '--out', out / 'inputs')
    shutil.rmtree(work)
    # run_io_matrix records the folders it read, here this build's temporary clones (gone, and named after the process):
    # record the cache entries they were cloned from instead, so the file is the same on every build.
    pin = out / 'inputs' / 'inputs.json'
    d = json.loads(pin.read_text())
    d.update(block_inputs=str(block), fio_preparation=str(fio))
    pin.write_text(json.dumps(d, indent=1, sort_keys=True) + '\n')


def matrix_inputs() -> pathlib.Path:
    """tests/io/run_io_matrix.py prepare's folder (mmio): the block inputs' root plus fio, on a 2 GiB ext4 image. Its
    inputs.json, linux and rootfs/bin/busybox are what run_serial_stress.py and run_io_soak.py read."""
    t, io, lib = _tools(), _io(), _lib()
    srcs = [str(fio_prep()), str(block_inputs()), inspect.getsource(writable_clone)]
    return resources.derived('io-matrix-inputs', _matrix_recipe,
                             srcs + [io / 'run_io_matrix.py', io / 'run_fio_admission.py', io / 'run_fio_baselines.py',
                                     io / 'run_virtio_block.py', io / 'run_virtio_rng.py', lib / 'io_common.py']) / 'inputs'


def _io5_recipe(out: pathlib.Path, inputs: list) -> None:
    image, python = map(pathlib.Path, inputs[:2])
    d = out / 'inputs'; d.mkdir()
    for f in (image, python): cache.clone(f, d / f.name)        # where prepare looks first: it downloads nothing
    _runner('run_io5.py', 'prepare', '--inputs', d)
    (d / image.name).unlink()                                   # disk.raw is extracted; run never reads the tarball


def io5_inputs() -> pathlib.Path:
    """tests/io/run_io5.py prepare's folder: the Debian 13 nocloud image's disk.raw with its /boot kernel and initramfs
    (taken out with Homebrew's debugfs) and the CPython 3.13.18 source, from the two pinned downloads
    (debian-cloud-nocloud, cpython-src). prepare checks the image's sha512 and the kernel's sha256 itself."""
    t, io, lib = _tools(), _io(), _lib()
    prefix, version = e2fsprogs()
    srcs = [str(resources.fetch('debian-cloud-nocloud')), str(resources.fetch('cpython-src')), str(prefix), version]   # paths: keyed by their pins
    return resources.derived('io5-inputs', _io5_recipe,
                             srcs + [io / 'run_io5.py', lib / 'io_common.py', io / 'run_net_linux.py', t / 'raw_root.py']) / 'inputs'


def _paper_recipe(out: pathlib.Path, inputs: list) -> None:
    import urllib.parse
    jar, mojang, *debs = map(pathlib.Path, inputs)
    d = out / 'inputs'; (d / 'debs').mkdir(parents=True)
    cache.clone(jar, d / 'paper.jar'); cache.clone(mojang, d / 'mojang.jar')
    for f in debs: cache.clone(f, d / 'debs' / urllib.parse.unquote(f.name))
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    (d / 'debs.sha256').write_text(''.join(f'{sha(f)}  {f.name}\n' for f in sorted((d / 'debs').iterdir())))


def paper_inputs() -> pathlib.Path:
    """tests/bench/run_paper.py's inputs, so its guest needs no network: the Paper jar (paper-jar), Mojang's server jar it
    patches (minecraft-server-26.3) and every `paper-deb-*` package (the JDK and what it needs beyond the IO5 image),
    with debs.sha256 listing the packages. All pinned downloads (tests/resources.toml)."""
    names = ['paper-jar', 'minecraft-server-26.3'] + sorted(n for n in resources.declarations() if n.startswith('paper-deb-'))
    return resources.derived('paper-inputs', _paper_recipe, [str(resources.fetch(n)) for n in names]) / 'inputs'


def recipe_sha256(recipe: pathlib.Path) -> str:
    """sha256 of the recipe folder's `sha256sum` listing (every file, sorted relative paths)."""
    files = sorted(p for p in recipe.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    listing = ''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(recipe).as_posix()}\n' for p in files)
    return hashlib.sha256(listing.encode()).hexdigest()




def _stress_recipe(out: pathlib.Path, inputs: list) -> None:
    recipe = pathlib.Path(inputs[0])
    _in_container('stress-ng', recipe, out, memory='8g')
    b = (out / 'bin/stress-ng').read_bytes()
    if b[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', b, 18)[0] != 62: raise BuildFailure('stress-ng', 'not an x86-64 ELF')
    ph, size, count = struct.unpack_from('<Q', b, 32)[0], *struct.unpack_from('<HH', b, 54)
    if any(struct.unpack_from('<I', b, ph + i * size)[0] in (2, 3) for i in range(count)): raise BuildFailure('stress-ng', 'not fully static')
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    (out / 'provenance.json').write_text(json.dumps({
        'binary_sha256': sha(out / 'bin/stress-ng'), 'source_sha256': sha(out / 'source.tar.xz'), 'recipe_sha256': recipe_sha256(recipe),
        'builder': artifacts.build_env_described()}, indent=1) + '\n')


def stress_ng() -> pathlib.Path:
    """Debian's stress-ng 0.19.02-1, static (tools/io/stress-ng): bin/stress-ng, source.tar.xz (the orig tarball the
    runners pin) and provenance.json (run_io_soak.py / run_serial_stress.py --stress-provenance)."""
    recipe = _tools() / 'io' / 'stress-ng'
    return resources.derived('io-stress-ng', _stress_recipe, [recipe, artifacts.build_env()])


# ---------------------------------------------------------------- campaign images (tools/correctness/recipes)
CAMPAIGN_RECIPES = 'correctness/recipes'
CAMPAIGN_COMMON = ('correctness/recipes/common.sh', 'correctness/recipes/common', 'correctness/init')
CAMPAIGN_TIMEOUT = 4 * 3600
PIN_LINE = re.compile(r'([0-9a-f]{64})  (\S+)')


def campaign_sources(suite: str) -> list[str]:
    """The tools/-relative files and folders the suite's recipe reads: its folder, common.sh, the common pins, the
    guest's /init and the files its uses.txt names. They are all the container sees, and they key the image."""
    t, name = _tools(), f'campaign-{suite}'
    d = t / CAMPAIGN_RECIPES / suite
    if not (d / 'build.sh').is_file(): raise BuildFailure(name, f'no recipe {d}/build.sh')
    uses = [l.split('#')[0].strip() for l in (d / 'uses.txt').read_text().splitlines()] if (d / 'uses.txt').is_file() else []
    rels = sorted({f'{CAMPAIGN_RECIPES}/{suite}', *CAMPAIGN_COMMON, *filter(None, uses)})
    if missing := [r for r in rels if not (t / r).exists()]: raise BuildFailure(name, f'{d}/uses.txt names missing files: {missing}')
    return rels


def read_pins(folder: pathlib.Path) -> dict[str, tuple[str, list[str]]]:
    """FILE -> (sha256, [pin files]) over every *.sha256 under `folder` (`SHA256  FILE` lines, sha256sum's format)."""
    pins: dict[str, tuple[str, list[str]]] = {}
    for pf in sorted(folder.rglob('*.sha256')):
        rel = pf.relative_to(folder).as_posix()
        for n, line in enumerate(pf.read_text().splitlines(), 1):
            m = PIN_LINE.fullmatch(line)
            if not m: raise BuildFailure('campaign', f'{pf}:{n}: not "SHA256  FILE": {line!r}')
            sha, files = pins.get(m[2], (m[1], []))
            if sha != m[1]: raise BuildFailure('campaign', f'{m[2]} is pinned twice, differently: {files[0]} and {rel}')
            pins[m[2]] = (sha, files + [rel])
    return pins


def campaign_manifest(name: str, downloads: pathlib.Path, pins: dict) -> list[dict]:
    """The manifest's sources: every download of the build (downloads.txt, `URL SHA256 FILE` lines) with its URL, sha256
    and pin files. A download no pin names or with another hash, or a pin nothing downloaded, fails the build."""
    sources, seen = [], set()
    for n, line in enumerate(downloads.read_text().splitlines(), 1):
        w = line.split()
        if len(w) != 3: raise BuildFailure(name, f'downloads.txt:{n}: not "URL SHA256 FILE": {line!r}')
        url, sha, f = w
        if f not in pins: raise BuildFailure(name, f'{f} ({url}) was downloaded, but no pin file names it')
        if pins[f][0] != sha: raise BuildFailure(name, f'{f} ({url}) has sha256 {sha}, pinned {pins[f][0]} in {pins[f][1]}')
        if f not in seen: seen.add(f); sources.append({'file': f, 'url': url, 'sha256': sha, 'pins': pins[f][1]})
    if unused := sorted(set(pins) - seen): raise BuildFailure(name, f'pinned but not downloaded (stale pins?): {unused}')
    return sorted(sources, key=lambda s: s['file'])


def campaign_boot_files(name: str, work: pathlib.Path, suite: str) -> list[str]:
    """`<suite>-boot.txt` (`PART [kernel=FILE] WORD...`: testkit/harness/campaign.py's per-part boot) and every kernel
    FILE it names, when the recipe wrote one; [] otherwise. A named kernel the recipe did not write fails the build."""
    f = work / f'{suite}-boot.txt'
    if not f.is_file(): return []
    kernels = sorted({w.split('=', 1)[1] for line in f.read_text().splitlines() for w in line.split()[1:] if w.startswith('kernel=')})
    if bad := [k for k in kernels if '/' in k or not (work / k).is_file()]:
        raise BuildFailure(name, f'{f.name} names kernels the recipe did not write: {bad}')
    return [f.name, *kernels]


def _campaign_recipe(out: pathlib.Path, inputs: list) -> None:
    suite, builder = inputs[:2]
    pairs = list(zip(inputs[3::2], inputs[4::2]))
    name, src, work = f'campaign-{suite}', out / 'src', out / 'work'
    for rel, p in pairs:                    # exactly the keyed files, laid out as in tools/: the container's /recipe
        dest = src / rel; dest.parent.mkdir(parents=True, exist_ok=True)
        if p.is_dir(): shutil.copytree(p, dest, ignore=shutil.ignore_patterns('__pycache__'))
        else: shutil.copy2(p, dest)
    work.mkdir()
    try:
        _in_container(name, src, work, memory='8g', timeout=CAMPAIGN_TIMEOUT, script=f'{CAMPAIGN_RECIPES}/{suite}/build.sh',
                      pins_to=_tools() / CAMPAIGN_RECIPES)
    except (BuildFailure, resources.Unpreparable) as e:
        if (work / 'log').is_dir():          # the failed entry is deleted: keep its logs where the message says
            keep = paths.state_dir() / 'build-logs' / f'{name}-{time.strftime("%Y%m%d-%H%M%S")}'
            keep.parent.mkdir(parents=True, exist_ok=True); shutil.copytree(work / 'log', keep)
            e.args = (f'{e.args[0]} (logs kept in {keep})',) + tuple(e.args[1:])
            if hasattr(e, 'reason'): e.reason = f'{e.reason} (logs kept in {keep})'
        raise
    image, listing = f'{suite}.cpio.zst', f'{suite}-cases.txt'
    for f in (image, listing, 'downloads.txt'):
        if not (work / f).is_file(): raise BuildFailure(name, f'the recipe wrote no {f}')
    lines = [l.split() for l in (work / listing).read_text().splitlines() if l.strip()]
    if not lines or any(len(w) != 2 for w in lines): raise BuildFailure(name, f'{listing} is not "PART TEST" lines')
    if len({w[1] for w in lines}) != len(lines): raise BuildFailure(name, f'{listing} names a test twice')
    sources = campaign_manifest(name, work / 'downloads.txt', read_pins(src / CAMPAIGN_RECIPES))
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    for f in (image, listing): os.replace(work / f, out / f)
    boot = campaign_boot_files(name, work, suite)     # optional: per-part kernels and command-line words (public)
    for f in boot: os.replace(work / f, out / f)
    if (work / 'log').is_dir(): os.replace(work / 'log', out / 'log')
    oracle = {}
    if (work / 'oracle').is_dir():           # litmus: herd7's x86-TSO states per family (tests/correctness/verdicts.py)
        os.replace(work / 'oracle', out / 'oracle')
        oracle = {'oracle': {p.name: sha(p) for p in sorted((out / 'oracle').iterdir())}}
    (out / 'manifest.json').write_text(json.dumps({
        'suite': suite, 'builder': artifacts.build_env_described(),
        'image': {'file': image, 'sha256': sha(out / image), 'bytes': (out / image).stat().st_size},
        'list': {'file': listing, 'sha256': sha(out / listing), 'tests': len(lines), 'parts': list(dict.fromkeys(w[0] for w in lines))},
        **({'boot': {f: sha(out / f) for f in boot}} if boot else {}),
        'recipe': {rel: recipe_sha256(p) if p.is_dir() else sha(p) for rel, p in ((r, src / r) for r, _ in pairs)},
        'built': _built(out / 'log' / 'built.sha256'),
        'sources': sources, **oracle}, indent=1) + '\n')
    shutil.rmtree(src); shutil.rmtree(work)


def _built(listing: pathlib.Path) -> list[dict]:
    """What a recipe built, as it recorded it (log/built.sha256, `SHA256  FILE` lines): recorded, never pinned."""
    if not listing.is_file(): return []
    return [{'file': m[2], 'sha256': m[1]} for m in map(PIN_LINE.fullmatch, listing.read_text().splitlines()) if m]


def campaign_image(suite: str) -> pathlib.Path:
    """The initramfs tools/correctness boots for a campaign, built here from tools/correctness/recipes/<suite> in the
    amd64 Debian builder (common.sh): a folder with <suite>.cpio.zst, <suite>-cases.txt (`PART TEST` per test, PART the
    kernel's part= word; also /ct/<suite>-cases.txt in the image), manifest.json (every download with its URL and sha256,
    the recipe files' hashes), log/ and, where the recipe writes one, oracle/ (litmus: herd7's states)."""
    t, io, lib = _tools(), _io(), _lib()
    pairs = [x for rel in campaign_sources(suite) for x in (rel, t / rel)]
    return resources.derived(f'campaign-{suite}', _campaign_recipe,
                             [suite, artifacts.build_env(), inspect.getsource(campaign_manifest) + inspect.getsource(read_pins), *pairs])


def _corpus_recipe(out: pathlib.Path, inputs: list) -> None:
    _runner(f'reference/{inputs[0]}', '-o', out / 'programs.json')


def corpus(generator: str) -> pathlib.Path:
    """programs.json from a tools/reference generator at its default seed (programs only: answers come from locks)."""
    return resources.derived('corpus', _corpus_recipe, [generator, _tools() / 'reference', _clang()]) / 'programs.json'


def components() -> dict[str, pathlib.Path]:
    """Each root path and the cache file or folder that provides it (building or fetching what is missing)."""
    return {NETBOOT_LINUX: resources.fetch('debian-netboot-linux'), NETBOOT_INITRD: resources.fetch('debian-netboot-initrd'),
            QEMU_USER: artifacts.qemu_user(), BLOCK_INPUTS: block_inputs(), BLOCK_KERNEL_DEB: resources.fetch('debian-kernel-deb'),
            NETWORK_ROOTFS: network_rootfs(), I7_PROGRAMS: corpus('gen_i7.py'), S2D_PROGRAMS: corpus('gen_slice2.py'),
            FIO_PREP: fio_prep(), STRESS: stress_ng(), MATRIX: matrix_inputs(), PLATFORM: platform_inputs(),
            CONSOLE: console_inputs(), CONTROL: control_inputs(), DISPLAY: display_inputs()}


def root() -> pathlib.Path:
    """The input root (a cache entry keyed by its components, which are content-addressed entries themselves). Files are
    hard links to the components', so the root costs no space and survives their eviction."""
    parts = components()
    assert set(parts) == set(PATHS)
    k = cache.key('input-root-v1', *(f'{rel}={src}' for rel, src in sorted(parts.items())))

    def build(out: pathlib.Path):
        for rel, src in parts.items():
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir(): shutil.copytree(src, dest, symlinks=True, copy_function=os.link)
            else: os.link(src, dest)

    return cache.get_or_build('input-root', k, build)
