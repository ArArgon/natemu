"""Built and fetched tools the harnesses need, each a cache entry keyed by what it is built from (spec R8.3): natemu's
binaries (signed), the fexc compiler at the FEX pin, cargo-nextest and the QEMU reference binary."""
import dataclasses
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys

from testkit import cache, paths, resources
from testkit.prepare import BuildFailure

CARGO = ['cargo', '+1.97.0', 'build', '--offline', '--locked', '--release']
NATEMU_SOURCES = ['crates', 'Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', '.cargo']


def _git(*a, check=True) -> str:
    return subprocess.run(['git', '-C', str(paths.repo_root()), *a], capture_output=True, text=True, check=check).stdout


def source_key(rel_paths: list[str], *extra: str) -> str:
    """Identity of the worktree's sources under `rel_paths`: the index's blob hashes plus the bytes of every file that
    differs from HEAD or is untracked there, so uncommitted edits make a new key."""
    root = paths.repo_root()
    parts = [_git('ls-files', '-s', '--', *rel_paths)]
    changed = set(_git('diff', '--name-only', 'HEAD', '--', *rel_paths).split()) | \
        set(_git('ls-files', '--others', '--exclude-standard', '--', *rel_paths).split())
    for f in sorted(changed):
        p = root / f
        parts.append(f)
        parts.append(p.read_bytes() if p.is_file() else b'<deleted>')
    return cache.key(*parts, *extra)


@dataclasses.dataclass(frozen=True)
class Natemu:
    entry: pathlib.Path
    key: str

    @property
    def host(self): return self.entry / 'release/natemu-host'

    @property
    def stub(self): return self.entry / 'release/natemu'

    @property
    def runtime(self): return self.entry / 'aarch64-unknown-none/release/natemu-runtime'


def _run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise BuildFailure(' '.join(map(str, cmd[:6])), (r.stderr or r.stdout)[-1500:])
    return r


def _pinned(name: str):
    """What preparation resolved for this run (TESTKIT_ARTIFACTS), so every worker of a run uses the same binaries even
    if the worktree changes while it runs."""
    import json
    return json.loads(os.environ.get('TESTKIT_ARTIFACTS', '{}')).get(name)


def pin_for_run(name: str, value) -> None:
    import json
    d = json.loads(os.environ.get('TESTKIT_ARTIFACTS', '{}'))
    d[name] = value
    os.environ['TESTKIT_ARTIFACTS'] = json.dumps(d)


def natemu() -> Natemu:
    """The release host, the `natemu` stub and the resident, built in the worktree's target/ and copied, signed, into
    an entry laid out like a cargo target folder."""
    if p := _pinned('natemu'): return Natemu(pathlib.Path(p['entry']), p['key'])
    k = source_key(NATEMU_SOURCES, 'natemu-v1')
    root = paths.repo_root()

    def build(out: pathlib.Path):
        # A private target folder per worktree: never the worktree's target/, which old runners and other runs execute from.
        target = paths.cache_root() / 'build-tmp' / ('target-' + hashlib.sha256(str(root).encode()).hexdigest()[:12])
        env = dict(os.environ, CARGO_TARGET_DIR=str(target))
        _run(['cargo', '+1.97.0', 'fetch', '--locked'], cwd=root, env=env)      # a new machine, or new dependencies (R1.3)
        _run(CARGO + ['-p', 'natemu-host'], cwd=root, env=env)
        _run(CARGO + ['-p', 'natemu-cli', '--bin', 'natemu'], cwd=root, env=env)
        _run(CARGO + ['-p', 'natemu-runtime', '--target', 'aarch64-unknown-none'], cwd=root, env=env)
        if source_key(NATEMU_SOURCES, 'natemu-v1') != k:
            raise BuildFailure('natemu', 'the sources changed while building; run again')
        for rel in ('release/natemu-host', 'release/natemu', 'aarch64-unknown-none/release/natemu-runtime'):
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target / rel, out / rel)
        for rel in ('release/natemu-host', 'release/natemu'):
            _run(['codesign', '--force', '--sign', '-', '--entitlements', root / 'crates/host/entitlements.plist', out / rel])

    n = Natemu(cache.get_or_build('natemu', k, build), k)
    pin_for_run('natemu', {'entry': str(n.entry), 'key': k})
    return n


def fexc() -> pathlib.Path:
    """The fexc compiler at the committed FEX pin, built by tools/fexc/build.sh in a scratch folder; only the binary is
    kept."""
    if p := _pinned('fexc'): return pathlib.Path(p)
    pin = _git('rev-parse', 'HEAD:third_party/FEX').strip()
    k = source_key(['tools/fexc'], 'fexc', pin)
    root = paths.repo_root()

    def build(out: pathlib.Path):
        scratch = paths.cache_root() / 'build-tmp' / f'fexc-{k[:16]}-{os.getpid()}'
        shutil.rmtree(scratch, ignore_errors=True); scratch.parent.mkdir(parents=True, exist_ok=True)
        try:
            _run(['sh', root / 'tools/fexc/build.sh', scratch], cwd=root)
            shutil.copy2(scratch / 'obj/fexc', out / 'fexc')
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            subprocess.run(['git', '-C', str(root / 'third_party/FEX'), 'worktree', 'prune'], capture_output=True)

    f = cache.get_or_build('fexc', k, build) / 'fexc'
    pin_for_run('fexc', str(f))
    return f


def nextest() -> pathlib.Path:
    return resources.fetch('cargo-nextest')


def qemu_user() -> pathlib.Path:
    p = resources.fetch('qemu-user-static')
    sys.path.insert(0, str(paths.repo_root() / 'tools' / 'reference'))
    import ref
    if hashlib.sha256(p.read_bytes()).hexdigest() != ref.PINS['qemu-x86_64-static']:
        raise BuildFailure('qemu-user-static', 'extracted qemu-x86_64-static does not match ref.PINS')
    return p


def compile_cache(n: Natemu) -> pathlib.Path:
    """natemu's own compile cache for this build only (spec R8.8), so builds never share or evict each other's."""
    d = paths.cache_root() / 'compile-cache' / n.key
    d.mkdir(parents=True, exist_ok=True)
    return d


# Debian 13 (trixie) 20251117, amd64 variant of the multi-platform index, run through Rosetta.
BENCH_BUILDER = 'docker.io/library/debian@sha256:8f6a88feef3ed01a300dafb87f208977f39dccda1fd120e878129463f7fa3b8f'


def bench_image() -> pathlib.Path:
    """The benchmark initramfs (bench.cpio), built on this Mac from tools/bench/image in an amd64 Debian container (spec
    R1.3, R14.3): an environment input, cached per machine, keyed by the recipe and the builder image."""
    if p := _pinned('bench-image'): return pathlib.Path(p)
    recipe = paths.repo_root() / 'tools' / 'bench' / 'image'
    k = cache.key('bench-image', recipe, BENCH_BUILDER)

    def build(out: pathlib.Path):
        _run(['container', 'run', '--rm', '--arch', 'amd64', '--cpus', '4', '--memory', '8g',
              '--volume', f'{recipe}:/recipe:ro', '--volume', f'{out}:/work', BENCH_BUILDER, 'bash', '/recipe/container.sh'],
             timeout=4 * 3600)
        if not (out / 'bench.cpio').exists(): raise BuildFailure('bench-image', 'no bench.cpio produced')

    f = cache.get_or_build('bench-image', k, build) / 'bench.cpio'
    pin_for_run('bench-image', str(f))
    return f


RESOLVERS = {'natemu': lambda: [natemu().entry], 'fexc': lambda: [fexc()], 'nextest': lambda: [nextest()],
             'qemu-user': lambda: [qemu_user()], 'bench-image': lambda: [bench_image()]}


def resolve(name: str) -> list[pathlib.Path]:
    return RESOLVERS[name]()
