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
    """A build step. A tool that cannot be started, a timeout and a failure are all BuildFailures naming the command,
    raised `from None`: the OSError's or TimeoutExpired's traceback would list subprocess's arguments, the environment
    (tokens) among them."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    except OSError as e:
        raise BuildFailure(' '.join(map(str, cmd[:6])), f'could not start: {e.strerror or e}') from None
    except subprocess.TimeoutExpired as e:
        raise BuildFailure(' '.join(map(str, cmd[:6])), f'timed out after {e.timeout:g} s') from None
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


NATEMU_BINARIES = ('release/natemu-host', 'release/natemu', 'aarch64-unknown-none/release/natemu-runtime')


def _cargo_build(src: pathlib.Path, target: pathlib.Path) -> None:
    """The release host, the stub and the resident of the sources in `src`, into the cargo target folder `target`."""
    env = dict(os.environ, CARGO_TARGET_DIR=str(target))
    _run(['cargo', '+1.97.0', 'fetch', '--locked'], cwd=src, env=env)      # a new machine, or new dependencies (R1.3)
    _run(CARGO + ['-p', 'natemu-host'], cwd=src, env=env)
    _run(CARGO + ['-p', 'natemu-cli', '--bin', 'natemu'], cwd=src, env=env)
    _run(CARGO + ['-p', 'natemu-runtime', '--target', 'aarch64-unknown-none'], cwd=src, env=env)


def _install(src: pathlib.Path, target: pathlib.Path, out: pathlib.Path) -> None:
    """The three binaries cloned from `target` into `out`, laid out like a target folder; the host's two signed with the
    entitlements of the sources they were built from."""
    for rel in NATEMU_BINARIES:
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        cache.clone(target / rel, out / rel)
    for rel in ('release/natemu-host', 'release/natemu'):
        _run(['codesign', '--force', '--sign', '-', '--entitlements', src / 'crates/host/entitlements.plist', out / rel])


def natemu() -> Natemu:
    """The release host, the `natemu` stub and the resident, built in the worktree's target/ and copied, signed, into
    an entry laid out like a cargo target folder."""
    if p := _pinned('natemu'): return Natemu(pathlib.Path(p['entry']), p['key'])
    k = source_key(NATEMU_SOURCES, 'natemu-v1')
    root = paths.repo_root()

    def build(out: pathlib.Path):
        # A private target folder per worktree: never the worktree's target/, which old runners and other runs execute from.
        target = paths.cache_root() / 'build-tmp' / ('target-' + hashlib.sha256(str(root).encode()).hexdigest()[:12])
        _cargo_build(root, target)
        if source_key(NATEMU_SOURCES, 'natemu-v1') != k:
            raise BuildFailure('natemu', 'the sources changed while building; run again')
        _install(root, target, out)

    n = Natemu(cache.get_or_build('natemu', k, build), k)
    pin_for_run('natemu', {'entry': str(n.entry), 'key': k})
    return n


def perf_runtime() -> pathlib.Path:
    """The resident built with its `perf` feature (it accounts its time by bucket and its system instructions by kind,
    crates/runtime/src/perf.rs): a profile run passes it as `--runtime` with a performance log. Never a timing build."""
    if p := _pinned('perf-runtime'): return pathlib.Path(p)
    k = source_key(NATEMU_SOURCES, 'natemu-perf-runtime-v1')
    root = paths.repo_root()
    rel = 'aarch64-unknown-none/release/natemu-runtime'

    def build(out: pathlib.Path):
        target = paths.cache_root() / 'build-tmp' / ('target-' + hashlib.sha256(str(root).encode()).hexdigest()[:12] + '-perf')
        _run(CARGO + ['-p', 'natemu-runtime', '--target', 'aarch64-unknown-none', '--features', 'perf'], cwd=root,
             env=dict(os.environ, CARGO_TARGET_DIR=str(target)))
        cache.clone(target / rel, out / 'natemu-runtime')

    f = cache.get_or_build('natemu-perf-runtime', k, build) / 'natemu-runtime'
    pin_for_run('perf-runtime', str(f))
    return f


def natemu_at(rev: str) -> Natemu:
    """natemu as committed at `rev` (an older build to compare with: the M1 performance check's baseline). The same three
    binaries as natemu(), built by the same cargo commands and toolchain pin and signed the same way, from `git archive
    REV` in a scratch folder, so the worktree is never touched. Keyed by the commit's tree (which holds the sources,
    rust-toolchain.toml and .cargo that natemu()'s key covers), so revisions with the same tree share one build. The FEX
    submodule is not needed: natemu's crates do not read third_party/."""
    r = subprocess.run(['git', '-C', str(paths.repo_root()), 'rev-parse', '--verify', '--quiet', f'{rev}^{{tree}}'],
                       capture_output=True, text=True)
    if r.returncode: raise BuildFailure(f'natemu@{rev}', 'no such revision in this repository')
    tree = r.stdout.strip()
    if p := _pinned(f'natemu-at:{tree}'): return Natemu(pathlib.Path(p['entry']), p['key'])
    k = cache.key('natemu-at-v1', tree, *CARGO)
    root = paths.repo_root()

    def build(out: pathlib.Path):
        scratch = paths.cache_root() / 'build-tmp' / f'natemu-at-{tree[:12]}-{os.getpid()}'
        shutil.rmtree(scratch, ignore_errors=True); (scratch / 'src').mkdir(parents=True)
        try:
            arc = subprocess.run(['git', '-C', str(root), 'archive', '--format=tar', tree], capture_output=True)
            if arc.returncode: raise BuildFailure(f'natemu@{rev}', arc.stderr.decode(errors='replace')[-800:])
            subprocess.run(['tar', '-x', '-C', str(scratch / 'src')], input=arc.stdout, check=True)
            _cargo_build(scratch / 'src', scratch / 'target')
            _install(scratch / 'src', scratch / 'target', out)
            (out / 'source.txt').write_text(f'git archive of {rev} (tree {tree})\n')
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    n = Natemu(cache.get_or_build('natemu-at', k, build), k)
    pin_for_run(f'natemu-at:{tree}', {'entry': str(n.entry), 'key': k})
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
            cache.clone(scratch / 'obj/fexc', out / 'fexc')
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            subprocess.run(['git', '-C', str(root / 'third_party/FEX'), 'worktree', 'prune'], capture_output=True)

    f = cache.get_or_build('fexc', k, build) / 'fexc'
    pin_for_run('fexc', str(f))
    return f


def fexc_at(rev: str) -> pathlib.Path:
    """fexc as committed at `rev` (the replay check's old compiler, tools/fexc/replay.py): built by this checkout's build.sh
    from that revision's FEX pin and a `git archive` of its tools/fexc; keyed by both, so revisions that share them share
    one build."""
    root = paths.repo_root()
    ids = [subprocess.run(['git', '-C', str(root), 'rev-parse', '--verify', '--quiet', f'{rev}:{p}'], capture_output=True, text=True)
           for p in ('tools/fexc', 'third_party/FEX')]
    if any(r.returncode for r in ids): raise BuildFailure(f'fexc@{rev}', 'no such revision, or no compiler in it')
    tree, pin = (r.stdout.strip() for r in ids)

    def build(out: pathlib.Path):
        scratch = paths.cache_root() / 'build-tmp' / f'fexc-at-{tree[:12]}-{os.getpid()}'
        shutil.rmtree(scratch, ignore_errors=True); (scratch / 'tools').mkdir(parents=True)
        try:
            arc = subprocess.run(['git', '-C', str(root), 'archive', '--format=tar', tree], capture_output=True, check=True)
            subprocess.run(['tar', '-x', '-C', str(scratch / 'tools')], input=arc.stdout, check=True)
            _run(['sh', root / 'tools/fexc/build.sh', scratch / 'build'], cwd=root,
                 env=dict(os.environ, FEXC_PIN=pin, FEXC_TOOLS=str(scratch / 'tools')))
            cache.clone(scratch / 'build/obj/fexc', out / 'fexc')
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            subprocess.run(['git', '-C', str(root / 'third_party/FEX'), 'worktree', 'prune'], capture_output=True)

    # (this checkout's build.sh builds it, so it is part of the key)
    return cache.get_or_build('fexc-at', cache.key('fexc-at-v1', tree, pin, (root / 'tools/fexc/build.sh').read_bytes()), build) / 'fexc'


def base_rev() -> str:
    """The commit this checkout's work started from: its merge base with `main` (HEAD itself on main)."""
    r = subprocess.run(['git', '-C', str(paths.repo_root()), 'merge-base', 'HEAD', 'main'], capture_output=True, text=True)
    if r.returncode: raise BuildFailure('base revision', 'no merge base with main: ' + r.stderr.strip()[-300:])
    return r.stdout.strip()


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
    qemu-img, with the firmware in <entry>/share/qemu (where QEMU and tests/lib/qemucache.py look for it). Built in a scratch
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
    """This machine's settings (not committed; spec R9): config.toml in the cache folder (every checkout on the machine),
    overridden table by table by .natemu/config.toml in the worktree."""
    import tomllib
    out = {}
    for f in (paths.cache_root() / 'config.toml', paths.repo_root() / '.natemu' / 'config.toml'):
        if f.exists():
            for k, v in tomllib.loads(f.read_text()).items():
                out[k] = {**out.get(k, {}), **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def container_builder() -> str:
    """Where the amd64 container builds (testkit.inputs._in_container: the input recipes, the campaign images) run:
    'local' (this Mac's Apple `container`, through Rosetta) or 'ssh:user@host' (the same pinned image, BENCH_BUILDER,
    under Docker on an x86-64 Linux machine: native speed). NATEMU_CONTAINER_BUILDER, else [build] builder in the
    machine's or the worktree's config.toml."""
    b = os.environ.get('NATEMU_CONTAINER_BUILDER') or local_config().get('build', {}).get('builder', 'local')
    if b != 'local' and not b.startswith('ssh:'):
        raise BuildFailure('container-builder', f'unknown builder {b!r}: local or ssh:user@host')
    return b


def build_env_described() -> str:
    b = container_builder()
    return f'local: {BENCH_BUILDER} (amd64 through Rosetta)' if b == 'local' else f'{BENCH_BUILDER} under Docker on {b[4:]} (native)'


def build_env() -> str:
    """What a container-built input is keyed by: an equivalent build environment, not identical bytes (builds are not
    byte-reproducible even on one machine: timestamps, temporary paths). Its parts:
    - the pinned image, BENCH_BUILDER: the whole userland the recipes build with;
    - this Mac's builds (the amd64 image through Rosetta) are only that, and every other key says 'native', so an entry
      built on an x86 machine is never taken for one built here, nor the other way round: Rosetta's CPU is not the
      x86 machine's, and recipes can detect CPU features at build time or record outputs of programs run there;
    - on an x86 machine, its CPU vendor and model name, and a digest of its CPU flags (the whole `flags` line, sorted:
      what configure scripts and test programs can see), for the same reason;
    - and its kernel release: the container runs on that kernel, which build-time checks and recorded outputs can see.
    Not its address, nor its CPU count (only how fast it builds): two equivalent machines share their entries, and
    which one built an entry is recorded in its provenance (build_env_described). Read from the machine once per run."""
    b = container_builder()
    if b == 'local': return BENCH_BUILDER
    if in_run := bool(os.environ.get('TESTKIT_RUN_DIR')):
        if p := _pinned(f'build-env:{b}'): return p
    env = f'{BENCH_BUILDER} {_native_env(b[4:])}'
    if in_run: pin_for_run(f'build-env:{b}', env)
    return env


_native_envs: dict[str, str] = {}


def _native_env(host: str) -> str:
    """build_env()'s description of the x86 machine `host`, read over ssh once per process. The last one read is kept
    in the state folder and used when the machine cannot be reached, so what is already built is still found."""
    if host in _native_envs:
        if isinstance(e := _native_envs[host], BuildFailure): raise e      # unreachable: tried once, not per input
        return e
    saved = paths.state_dir() / 'builder-env' / host
    try:
        out = _run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', host,
                    "uname -r && grep -m3 -E '^(vendor_id|model name|flags)[[:space:]]*:' /proc/cpuinfo"], timeout=60).stdout
    except BuildFailure as e:
        # ponytail: if the machine comes back (say, rebooted into a new kernel) before this run builds on it, that build
        # is stored under the last description; re-describe in _in_docker_over_ssh if that ever matters
        last = saved.read_text().strip() if saved.is_file() else ''
        _native_envs[host] = last if last.startswith('native: ') else e
        return _native_env(host)
    env = _describe_native(out)                 # not a description: a failure, never the saved one
    saved.parent.mkdir(parents=True, exist_ok=True)
    tmp = saved.with_name(f'.{host}.{os.getpid()}'); tmp.write_text(env + '\n'); os.replace(tmp, saved)
    _native_envs[host] = env
    return env


def _describe_native(out: str) -> str:
    """`uname -r` and /proc/cpuinfo's vendor_id, model name and flags lines -> build_env()'s native part."""
    lines = out.strip().splitlines()
    info = {k.strip(): v.strip() for k, _, v in (l.partition(':') for l in lines[1:])}
    if not lines or any(k not in info for k in ('vendor_id', 'model name', 'flags')):
        raise BuildFailure('container-builder', f'cannot describe the builder from {out[-500:]!r}')
    flags = hashlib.sha256(' '.join(sorted(set(info['flags'].split()))).encode()).hexdigest()[:16]
    return f"native: {info['vendor_id']} {info['model name']}, flags {flags}, kernel {lines[0].strip()}"


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
    """FEX's upstream ASM unit tests (unittests/ASM: the .asm files, Includes and the known-failure lists; beside it
    unittests/32Bit_ASM, the 32-bit suite, which a case reaches as `{fex_asm}/../32Bit_ASM`) at the committed FEX pin, extracted from the submodule's git objects, which every worktree shares through the main repository, so a
    fresh worktree's empty third_party/FEX needs no fetch. Initialises the submodule only if those objects are missing."""
    if p := _pinned('fex-asm'): return pathlib.Path(p)
    try: return _fex_asm()
    except subprocess.CalledProcessError as e:      # a build failure of the cases that need it, never of the whole run
        raise BuildFailure('fex-asm', f'{" ".join(map(str, e.cmd[:4]))}: exit {e.returncode}: '
                                      f'{(e.stderr or b"")[-800:]!r}') from None


def _fex_asm() -> pathlib.Path:
    pin = _git('rev-parse', 'HEAD:third_party/FEX').strip()
    root = paths.repo_root()

    def build(out: pathlib.Path):
        common = pathlib.Path(_git('rev-parse', '--path-format=absolute', '--git-common-dir').strip())
        gd = common / 'modules' / 'third_party' / 'FEX'
        if subprocess.run(['git', f'--git-dir={gd}', 'cat-file', '-e', f'{pin}^{{commit}}'], capture_output=True).returncode:
            _run(['git', '-C', root, 'submodule', 'update', '--init', '--depth', '1', 'third_party/FEX'], cwd=root)
            gd = pathlib.Path(subprocess.run(['git', '-C', str(root / 'third_party/FEX'), 'rev-parse', '--absolute-git-dir'],
                                             capture_output=True, text=True, check=True).stdout.strip())
        arc = subprocess.run(['git', f'--git-dir={gd}', 'archive', pin, 'unittests/ASM', 'unittests/32Bit_ASM'], capture_output=True, check=True).stdout
        subprocess.run(['tar', 'x', '-C', str(out)], input=arc, check=True)

    d = cache.get_or_build('fex-asm', hashlib.sha256(f'fex-asm+32 {pin}'.encode()).hexdigest(), build) / 'unittests' / 'ASM'
    pin_for_run('fex-asm', str(d))
    return d

RESOLVERS = {'natemu': lambda: [natemu().entry], 'fexc': lambda: [fexc()], 'nextest': lambda: [nextest()],
             'qemu-user': lambda: [qemu_user()], 'bench-image': lambda: [bench_image()],
             'qemu-system': lambda: [qemu_system()], 'fex-asm': lambda: [fex_asm()],
             'perf-runtime': lambda: [perf_runtime()]}


def resolve(name: str) -> list[pathlib.Path]:
    return RESOLVERS[name]()
