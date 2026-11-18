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


# QEMU system mode as the runners use it: TCG only, headless (-nographic / -display none), the pc machine with its
# default VGA (pixman), user networking (slirp) and qemu-img. --without-default-features keeps optional libraries that
# happen to be installed out of the build, so every Mac builds the same feature set; --disable-download keeps meson to
# the subprojects shipped in the tarball.
QEMU_CONFIGURE = ['--target-list=x86_64-softmmu', '--without-default-features', '--enable-tools', '--enable-pixman',
                  '--enable-slirp', '--enable-zstd', '--disable-docs', '--disable-gtk', '--disable-sdl', '--disable-cocoa',
                  '--disable-download']
QEMU_BUILD_DEPS = ['glib', 'pixman', 'pkgconf', 'ninja', 'libslirp', 'zstd']     # Homebrew's, not pinned
QEMU_WHEELS = ['setuptools-wheel', 'wheel-wheel']        # resources configure's build venv needs (tests/resources.toml)


def qemu_system() -> pathlib.Path:
    """The bin folder of QEMU 11.1.2 built from the pinned release tarball (resource qemu-src): qemu-system-x86_64 and
    qemu-img, with the firmware in <entry>/share/qemu (where QEMU and tools/qemucache.py look for it). Built in a scratch
    folder, installed into the entry; Homebrew's libraries are the build dependencies, their versions in build-info.txt."""
    import inspect
    if p := _pinned('qemu-system'): return pathlib.Path(p)
    decls = resources.declarations()
    for r in ('qemu-src', *QEMU_WHEELS):
        if r not in decls: raise resources.Unpreparable(r, 'not declared in tests/resources.toml')
    decl = decls['qemu-src']
    k = cache.key('qemu-system', *(decls[r]['sha256'] for r in ('qemu-src', *QEMU_WHEELS)), *QEMU_CONFIGURE,
                  inspect.getsource(qemu_system))

    def build(out: pathlib.Path):
        brew = shutil.which('brew') or '/opt/homebrew/bin/brew'
        if not os.access(brew, os.X_OK):
            raise BuildFailure('qemu-system', 'needs Homebrew for its build dependencies: brew install ' + ' '.join(QEMU_BUILD_DEPS))
        prefix = subprocess.run([brew, '--prefix'], capture_output=True, text=True, check=True).stdout.strip()
        versions = subprocess.run([brew, 'list', '--versions', *QEMU_BUILD_DEPS], capture_output=True, text=True).stdout
        missing = [d for d in QEMU_BUILD_DEPS if not any(l.split()[:1] == [d] for l in versions.splitlines())]
        if missing:
            raise BuildFailure('qemu-system', 'missing Homebrew build dependencies: brew install ' + ' '.join(missing))
        tarball, wheels = resources.fetch('qemu-src'), [resources.fetch(r) for r in QEMU_WHEELS]
        scratch = paths.cache_root() / 'build-tmp' / f'qemu-{k[:16]}-{os.getpid()}'
        shutil.rmtree(scratch, ignore_errors=True); scratch.mkdir(parents=True)
        env = dict(os.environ, PATH=f'{prefix}/bin:/usr/bin:/bin:/usr/sbin:/sbin')
        try:
            _run(['tar', '-xf', tarball, '-C', scratch])
            src = next(scratch.glob('qemu-*'))
            for w in wheels: shutil.copy(w, src / 'python' / 'wheels' / w.name)
            (src / 'build').mkdir(exist_ok=True)
            _run([src / 'configure', f'--prefix={out}', *QEMU_CONFIGURE], cwd=src / 'build', env=env)
            _run(['make', f'-j{os.cpu_count() or 4}'], cwd=src / 'build', env=env)
            _run(['make', 'install'], cwd=src / 'build', env=env)
            cc = subprocess.run(['cc', '--version'], capture_output=True, text=True, env=env).stdout.splitlines()[:1]
            py = subprocess.run(['python3', '--version'], capture_output=True, text=True, env=env).stdout.strip()
            (out / 'build-info.txt').write_text(
                f'source: {decl["urls"][0]} sha256 {decl["sha256"]}\nconfigure: {" ".join(QEMU_CONFIGURE)}\n'
                f'compiler: {" ".join(cc)}\npython: {py}\nhomebrew ({prefix}, not pinned):\n{versions}')
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        for exe in ('qemu-system-x86_64', 'qemu-img'):
            if not (out / 'bin' / exe).is_file(): raise BuildFailure('qemu-system', f'make install left no bin/{exe}')

    b = cache.get_or_build('qemu-system', k, build) / 'bin'
    pin_for_run('qemu-system', str(b))
    return b


def compile_cache(n: Natemu) -> pathlib.Path:
    """natemu's own compile cache for this build only (spec R8.8), so builds never share or evict each other's."""
    d = paths.cache_root() / 'compile-cache' / n.key
    d.mkdir(parents=True, exist_ok=True)
    return d


# Debian 13 (trixie) 20251117, amd64 variant of the multi-platform index, run through Rosetta.
BENCH_BUILDER = 'docker.io/library/debian@sha256:8f6a88feef3ed01a300dafb87f208977f39dccda1fd120e878129463f7fa3b8f'


def local_config() -> dict:
    """This machine's settings: .natemu/config.toml in the worktree (not committed; spec R9)."""
    import tomllib
    f = paths.repo_root() / '.natemu' / 'config.toml'
    return tomllib.loads(f.read_text()) if f.exists() else {}


def bench_builder() -> str:
    """Where the benchmark programs are built: 'local' (this Mac, an amd64 container through Rosetta) or
    'ssh:user@host' (a real x86-64 Linux machine). NATEMU_BENCH_BUILDER, else [bench] builder in .natemu/config.toml."""
    b = os.environ.get('NATEMU_BENCH_BUILDER') or local_config().get('bench', {}).get('builder', 'local')
    if b != 'local' and not b.startswith('ssh:'):
        raise BuildFailure('bench-image', f'unknown builder {b!r}: local or ssh:user@host')
    return b


def _build_bench_local(recipe: pathlib.Path, out: pathlib.Path) -> None:
    _run(['container', 'run', '--rm', '--arch', 'amd64', '--cpus', '4', '--memory', '8g',
          '--volume', f'{recipe}:/recipe:ro', '--volume', f'{out}:/work', BENCH_BUILDER, 'bash', '/recipe/container.sh'],
         timeout=4 * 3600)


def _build_bench_ssh(recipe: pathlib.Path, out: pathlib.Path, hostname: str, ssh: str = 'ssh', scp: str = 'scp') -> None:
    """The recipe's own build.sh, natively on an x86-64 Linux machine (it needs apt-get, dpkg, curl, python3, binutils,
    tclsh there, as on the reference box), in a temporary folder that is removed afterwards: nothing is kept there."""
    opts = ['-o', 'BatchMode=yes']
    d = _run([ssh, *opts, hostname, 'mktemp -d "$HOME/.natemu-bench-XXXXXX"']).stdout.strip()   # on disk, not a RAM /tmp
    try:
        tar = subprocess.run(['tar', '-C', str(recipe), '-cf', '-', '.'], capture_output=True, check=True).stdout
        r = subprocess.run([ssh, *opts, hostname, f'tar -C {d} -xf -'], input=tar, capture_output=True)
        if r.returncode: raise BuildFailure('bench-image', r.stderr.decode()[-1000:])
        _run([ssh, *opts, hostname, f'bash {d}/scripts/build.sh {d}/b > {d}/build.log 2>&1 || {{ tail -40 {d}/build.log; exit 1; }}'],
             timeout=4 * 3600)
        for f in ('bench.cpio', 'expected.txt'):
            _run([scp, '-q', *opts, f'{hostname}:{d}/b/out/{f}', str(out / f)])
    finally:
        subprocess.run([ssh, *opts, hostname, f'rm -rf {d}'], capture_output=True)


def bench_image() -> pathlib.Path:
    """The benchmark initramfs (bench.cpio), built from tools/bench/image by the configured builder (spec R1.3, R14.3): an
    environment input, cached per machine, keyed by the recipe and the builder. The builder is recorded beside it."""
    if p := _pinned('bench-image'): return pathlib.Path(p)
    recipe = paths.repo_root() / 'tools' / 'bench' / 'image'
    builder = bench_builder()
    k = cache.key('bench-image', recipe, BENCH_BUILDER if builder == 'local' else builder)

    def build(out: pathlib.Path):
        if builder == 'local': _build_bench_local(recipe, out)
        else: _build_bench_ssh(recipe, out, builder[4:])
        if not (out / 'bench.cpio').exists(): raise BuildFailure('bench-image', 'no bench.cpio produced')
        (out / 'builder.txt').write_text(('local: ' + BENCH_BUILDER + ' (amd64 through Rosetta)' if builder == 'local'
                                          else builder) + '\n')

    f = cache.get_or_build('bench-image', k, build) / 'bench.cpio'
    pin_for_run('bench-image', str(f))
    return f



def fex_asm() -> pathlib.Path:
    """FEX's upstream ASM unit tests (unittests/ASM: the .asm files, Includes and the known-failure lists) at the committed
    FEX pin, extracted from the submodule's git objects, which every worktree shares through the main repository, so a
    fresh worktree's empty third_party/FEX needs no fetch. Initialises the submodule only if those objects are missing."""
    if p := _pinned('fex-asm'): return pathlib.Path(p)
    pin = _git('rev-parse', 'HEAD:third_party/FEX').strip()
    root = paths.repo_root()

    def build(out: pathlib.Path):
        common = pathlib.Path(_git('rev-parse', '--path-format=absolute', '--git-common-dir').strip())
        gd = common / 'modules' / 'third_party' / 'FEX'
        if subprocess.run(['git', f'--git-dir={gd}', 'cat-file', '-e', f'{pin}^{{commit}}'], capture_output=True).returncode:
            _run(['git', '-C', root, 'submodule', 'update', '--init', '--depth', '1', 'third_party/FEX'], cwd=root)
            gd = pathlib.Path(subprocess.run(['git', '-C', str(root / 'third_party/FEX'), 'rev-parse', '--absolute-git-dir'],
                                             capture_output=True, text=True, check=True).stdout.strip())
        arc = subprocess.run(['git', f'--git-dir={gd}', 'archive', pin, 'unittests/ASM'], capture_output=True, check=True).stdout
        subprocess.run(['tar', 'x', '-C', str(out)], input=arc, check=True)

    d = cache.get_or_build('fex-asm', hashlib.sha256(f'fex-asm {pin}'.encode()).hexdigest(), build) / 'unittests' / 'ASM'
    pin_for_run('fex-asm', str(d))
    return d

RESOLVERS = {'natemu': lambda: [natemu().entry], 'fexc': lambda: [fexc()], 'nextest': lambda: [nextest()],
             'qemu-user': lambda: [qemu_user()], 'bench-image': lambda: [bench_image()],
             'qemu-system': lambda: [qemu_system()], 'fex-asm': lambda: [fex_asm()]}


def resolve(name: str) -> list[pathlib.Path]:
    return RESOLVERS[name]()
