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
PATHS = (NETBOOT_LINUX, NETBOOT_INITRD, QEMU_USER, BLOCK_INPUTS, BLOCK_KERNEL_DEB, NETWORK_ROOTFS, I7_PROGRAMS, S2D_PROGRAMS,
         FIO_PREP, STRESS)
E2FSPROGS = pathlib.Path('/opt/homebrew/opt/e2fsprogs')     # Homebrew's: a host tool, checked (version and hashes key the image), not pinned


def _tools() -> pathlib.Path:
    return paths.repo_root() / 'tools'


def _runner(script: str, *args) -> None:
    artifacts._run([sys.executable, '-B', _tools() / script, *map(str, args)], cwd=paths.repo_root())


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
    """tools/run_virtio_rng.py prepare's folder: the netboot kernel, stock modules from the netboot initrd, Debian's BusyBox."""
    t = _tools()
    srcs = [str(resources.fetch(n)) for n in ('debian-netboot-linux', 'debian-netboot-initrd', 'busybox-static-deb')]
    return resources.derived('io-rng-inputs', _rng_recipe, srcs + [t / 'run_virtio_rng.py', t / 'io_common.py', t / 'play/mkcpio.py']) / 'rng'


def e2fsprogs() -> tuple[pathlib.Path, str]:
    """Homebrew's e2fsprogs (its Cellar folder, as tools/run_virtio_block.py records it) and its version."""
    prefix = pathlib.Path(os.environ.get('NATEMU_E2FSPROGS') or E2FSPROGS).resolve()
    if not all((prefix / 'sbin' / n).is_file() for n in ('mke2fs', 'e2fsck', 'debugfs')):
        raise BuildFailure('e2fsprogs', f'no mke2fs, e2fsck and debugfs under {prefix}/sbin: brew install e2fsprogs')
    return prefix, prefix.name


def _block_recipe(out: pathlib.Path, inputs: list) -> None:
    rng, kernel_deb, e2fs = inputs[:3]
    _runner('run_virtio_block.py', 'prepare', '--rng-inputs', rng, '--kernel-deb', kernel_deb, '--e2fsprogs', e2fs, '--out', out / 'inputs-01')


def block_inputs() -> pathlib.Path:
    """tools/run_virtio_block.py prepare's folder (mmio): the ext4 root image and its boot initramfs."""
    t = _tools()
    prefix, version = e2fsprogs()
    conf = prefix.parent.parent / 'etc/mke2fs.conf'         # the configuration prepare copies (its own fallback order)
    conf = conf if conf.exists() else pathlib.Path('/opt/homebrew/etc/mke2fs.conf')
    host = [prefix / 'sbin' / n for n in ('mke2fs', 'e2fsck', 'debugfs')] + ([conf] if conf.exists() else [])
    srcs = [str(rng_inputs()), str(resources.fetch('debian-kernel-deb')), str(prefix), version, _clang()]
    return resources.derived('io-block-inputs', _block_recipe,
                             srcs + host + [t / 'run_virtio_block.py', t / 'run_virtio_rng.py', t / 'io_common.py',
                                            t / 'play/mkcpio.py', t / 'reference/ref.py']) / 'inputs-01'


def _network_recipe(out: pathlib.Path, inputs: list) -> None:
    recipe, image = inputs[:2]
    work = out / 'work'; work.mkdir()
    artifacts._run(['container', 'run', '--rm', '--arch', 'amd64', '--cpus', '4', '--memory', '4g',
                    '--volume', f'{recipe}:/recipe:ro', '--volume', f'{work}:/work', image, 'bash', '/recipe/container.sh'],
                   timeout=3600)
    (out / 'rootfs').mkdir()
    artifacts._run(['tar', '-C', out / 'rootfs', '-xpf', work / 'rootfs.tar'])
    for n in ('packages.txt', 'debs.sha256'): shutil.copy2(work / n, out / n)
    shutil.rmtree(work)


def network_rootfs() -> pathlib.Path:
    """The iperf3/curl/openssl tree tools/run_net_linux.py copies into its guest: tools/io/network-rootfs, pinned Debian
    packages unpacked in an amd64 Debian container (the bench image's builder)."""
    if not shutil.which('container'): raise BuildFailure('network-rootfs', "needs Apple's `container` (brew install container)")
    recipe = _tools() / 'io' / 'network-rootfs'
    return resources.derived('io-network-rootfs', _network_recipe, [recipe, artifacts.BENCH_BUILDER]) / 'rootfs'


def _in_container(name: str, recipe: pathlib.Path, work: pathlib.Path, memory: str = '4g', timeout: int = 3600) -> None:
    """recipe/container.sh in the amd64 Debian builder (artifacts.BENCH_BUILDER, through Rosetta), /recipe read-only and
    /work = work. A download that fails is Unpreparable (the URL is in the message), anything else a BuildFailure."""
    if not shutil.which('container'): raise BuildFailure(name, "needs Apple's `container` (brew install container)")
    cname = f'natemu-build-{name}-{os.getpid()}'
    cmd = ['container', 'run', '--rm', '--name', cname, '--arch', 'amd64', '--cpus', '4', '--memory', memory,
           '--volume', f'{recipe}:/recipe:ro', '--volume', f'{work}:/work', artifacts.BENCH_BUILDER, 'bash', '/recipe/container.sh']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        try: subprocess.run(['container', 'kill', cname], capture_output=True, timeout=60)  # the client's death does not stop it
        except (OSError, subprocess.SubprocessError): pass
        raise BuildFailure(name, f'{recipe}/container.sh took more than {timeout} s')
    if r.returncode:
        tail = (r.stderr or r.stdout)[-1500:]
        repin = [f for f in ('packages.txt', 'debs.sha256', 'sources.sha256') if (work / f).is_file()]
        if repin:                    # re-pinning: keep the new pins before the failed entry is deleted
            keep = paths.state_dir() / 'repin' / name; keep.mkdir(parents=True, exist_ok=True)
            for f in repin: shutil.copy2(work / f, keep / f)
            raise BuildFailure(name, f'no pins in {recipe}: new ones written to {keep} ({", ".join(repin)}); check them, '
                                     f'copy them into {recipe} and build again')
        if re.search(r'Failed to fetch|Could not resolve|Temporary failure resolving|Unable to connect|Unable to fetch', tail):
            raise resources.Unpreparable(name, tail)
        raise BuildFailure(name, tail)


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
    return resources.derived('io-fio-prep', _fio_recipe, [recipe, artifacts.BENCH_BUILDER]) / 'fio-prep'


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
    """tools/run_io_matrix.py prepare's folder (mmio): the block inputs' root plus fio, on a 2 GiB ext4 image. Its
    inputs.json, linux and rootfs/bin/busybox are what run_serial_stress.py and run_io_soak.py read."""
    t = _tools()
    srcs = [str(fio_prep()), str(block_inputs()), inspect.getsource(writable_clone)]
    return resources.derived('io-matrix-inputs', _matrix_recipe,
                             srcs + [t / 'run_io_matrix.py', t / 'run_fio_admission.py', t / 'run_fio_baselines.py',
                                     t / 'run_virtio_block.py', t / 'run_virtio_rng.py', t / 'io_common.py']) / 'inputs'


def recipe_sha256(recipe: pathlib.Path) -> str:
    """sha256 of the recipe folder's `sha256sum` listing (every file, sorted relative paths)."""
    files = sorted(p for p in recipe.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    listing = ''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(recipe).as_posix()}\n' for p in files)
    return hashlib.sha256(listing.encode()).hexdigest()


STRESS_BUILD_SHA = '53140a759e4194faad69a11e1a95c0c868959cb3b93eb38e7dbac0bb8ed1a7cf'   # = run_io_soak.BUILD_SHA = run_serial_stress.BUILD_SHA


def _stress_recipe(out: pathlib.Path, inputs: list) -> None:
    recipe = pathlib.Path(inputs[0])
    _in_container('stress-ng', recipe, out, memory='8g')
    b = (out / 'bin/stress-ng').read_bytes()
    if b[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', b, 18)[0] != 62: raise BuildFailure('stress-ng', 'not an x86-64 ELF')
    ph, size, count = struct.unpack_from('<Q', b, 32)[0], *struct.unpack_from('<HH', b, 54)
    if any(struct.unpack_from('<I', b, ph + i * size)[0] in (2, 3) for i in range(count)): raise BuildFailure('stress-ng', 'not fully static')
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    if sha(out / 'bin/stress-ng') != STRESS_BUILD_SHA:
        raise BuildFailure('stress-ng', f'the build no longer reproduces {STRESS_BUILD_SHA} (got {sha(out / "bin/stress-ng")}): '
                                        'the runners accept only that binary')
    (out / 'provenance.json').write_text(json.dumps({
        'binary_sha256': sha(out / 'bin/stress-ng'), 'source_sha256': sha(out / 'source.tar.xz'), 'recipe_sha256': recipe_sha256(recipe),
        'builder': f'local: {artifacts.BENCH_BUILDER} (amd64 through Rosetta)'}, indent=1) + '\n')


def stress_ng() -> pathlib.Path:
    """Debian's stress-ng 0.19.02-1, static (tools/io/stress-ng): bin/stress-ng, source.tar.xz (the orig tarball the
    runners pin) and provenance.json (run_io_soak.py / run_serial_stress.py --stress-provenance)."""
    recipe = _tools() / 'io' / 'stress-ng'
    return resources.derived('io-stress-ng', _stress_recipe, [recipe, artifacts.BENCH_BUILDER])


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
            FIO_PREP: fio_prep(), STRESS: stress_ng()}


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
