"""The input root: one cache entry laid out like natemu-raw that holds exactly the inputs the old runners (tools/run_*.py)
read, each built on this machine from a recipe in the repo or a pinned download (spec R14), never copied. Run an old
runner against it with NATEMU_RAW_ROOT=<root()> and NATEMU_STATE_ROOT=<a writable folder>: the entry is read-only and
the runners' locks and caches go to the state root (tools/raw_root.py). Answers (expected.json) are not inputs: they
come from the locks."""
import os
import pathlib
import shutil
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
PATHS = (NETBOOT_LINUX, NETBOOT_INITRD, QEMU_USER, BLOCK_INPUTS, BLOCK_KERNEL_DEB, NETWORK_ROOTFS, I7_PROGRAMS, S2D_PROGRAMS)
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


def _corpus_recipe(out: pathlib.Path, inputs: list) -> None:
    _runner(f'reference/{inputs[0]}', '-o', out / 'programs.json')


def corpus(generator: str) -> pathlib.Path:
    """programs.json from a tools/reference generator at its default seed (programs only: answers come from locks)."""
    return resources.derived('corpus', _corpus_recipe, [generator, _tools() / 'reference', _clang()]) / 'programs.json'


def components() -> dict[str, pathlib.Path]:
    """Each root path and the cache file or folder that provides it (building or fetching what is missing)."""
    return {NETBOOT_LINUX: resources.fetch('debian-netboot-linux'), NETBOOT_INITRD: resources.fetch('debian-netboot-initrd'),
            QEMU_USER: artifacts.qemu_user(), BLOCK_INPUTS: block_inputs(), BLOCK_KERNEL_DEB: resources.fetch('debian-kernel-deb'),
            NETWORK_ROOTFS: network_rootfs(), I7_PROGRAMS: corpus('gen_i7.py'), S2D_PROGRAMS: corpus('gen_slice2.py')}


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
