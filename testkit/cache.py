"""The machine's content-addressed cache (spec R8): entries `<root>/<kind>/<key>/`, built once machine-wide, published
only when complete, protected by per-run leases and by roots while in use, and cleaned least-recently-used first."""
import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Callable, Iterable

from testkit import paths

SKIP_KINDS = {'state', 'locks', 'venv', 'build-tmp', 'compile-cache'}     # not entries: machine state, build locks, the framework's own environments


def key(*parts) -> str:
    """sha256 over the parts, each length-prefixed. A Path contributes its bytes; a folder its sorted relative names and
    file bytes (`__pycache__` left out)."""
    h = hashlib.sha256()

    def feed(b: bytes):
        h.update(len(b).to_bytes(8, 'little')); h.update(b)

    for p in parts:
        if isinstance(p, pathlib.Path):
            if p.is_dir():
                for f in sorted(x for x in p.rglob('*') if x.is_file() and '__pycache__' not in x.parts):
                    feed(f.relative_to(p).as_posix().encode()); feed(f.read_bytes())
            else:
                feed(p.read_bytes())
        else:
            feed(p if isinstance(p, bytes) else str(p).encode())
    return h.hexdigest()


_clone_warned = False


def clone(src: pathlib.Path, dest: pathlib.Path) -> None:
    """Copy `src` (a file or a folder) to `dest` as an APFS clone (`clonefile(2)`): the blocks are shared until one side
    writes, so a per-run image costs no space and no time. Symlinks stay symlinks. Where the volume cannot clone (not APFS,
    or `dest` on another volume) it copies, and says so once per process. `dest` must not exist."""
    global _clone_warned
    src, dest = pathlib.Path(src), pathlib.Path(dest)
    is_dir = src.is_dir() and not src.is_symlink()
    try:
        subprocess.run(['cp', '-cR' if is_dir else '-cP', str(src), str(dest)], check=True, capture_output=True)
        if not is_dir and not dest.is_symlink(): dest.chmod(dest.stat().st_mode | 0o200)
        return
    except (subprocess.CalledProcessError, OSError):
        if dest.is_symlink() or dest.is_file(): dest.unlink()
        elif dest.is_dir(): shutil.rmtree(dest, ignore_errors=True)
    if not _clone_warned:
        _clone_warned = True
        print(f'testkit: copied {src.name} (this volume cannot clone files; copies use space)', file=sys.stderr)
    if is_dir: shutil.copytree(src, dest, symlinks=True)
    else:
        shutil.copy2(src, dest, follow_symlinks=False)
        if not dest.is_symlink(): dest.chmod(dest.stat().st_mode | 0o200)


def entry_path(kind: str, k: str) -> pathlib.Path:
    return paths.cache_root() / kind / k


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0); return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@contextlib.contextmanager
def _flock(path: pathlib.Path, mode=fcntl.LOCK_EX):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, mode); yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


class Miss(Exception):
    """Raised instead of building while peeking (testkit.prepare's first pass)."""


_peeking = False


@contextlib.contextmanager
def peek():
    """Within this block a missing entry raises Miss instead of being built: what is already here costs no claim."""
    global _peeking
    old, _peeking = _peeking, True
    try: yield
    finally: _peeking = old


def get_or_build(kind: str, k: str, build: Callable[[pathlib.Path], None], *, size_hint: int = 0) -> pathlib.Path:
    """The entry, building it with `build(tmpdir)` if missing. One builder per key machine-wide; a waiter gets its result.
    A failed or killed build leaves nothing that is ever served."""
    entry = entry_path(kind, k)
    if entry.is_dir():
        os.utime(entry); return entry
    if _peeking: raise Miss(f'{kind}/{k[:12]}')
    with _flock(paths.cache_root() / 'locks' / f'{kind}-{k}'):
        if entry.is_dir():
            os.utime(entry); return entry
        for old in entry.parent.glob(f'.tmp-{k}-*'):        # a killed builder's leftovers (we hold the key's lock)
            shutil.rmtree(old, ignore_errors=True)
        tmp = entry.parent / f'.tmp-{k}-{os.getpid()}'
        tmp.mkdir(parents=True)
        try:
            build(tmp)
            _readonly(tmp)
            os.rename(tmp, entry)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True); raise
    return entry


def _readonly(p: pathlib.Path) -> None:
    """A published entry is read-only, so a stray build cannot relink files other runs are executing."""
    for dirpath, dirs, files in os.walk(p):
        for n in files:
            f = os.path.join(dirpath, n)
            if not os.path.islink(f): os.chmod(f, os.stat(f).st_mode & ~0o222)


def _rmtree(p: pathlib.Path) -> None:
    def fix(func, path, exc):
        os.chmod(os.path.dirname(path), 0o755); os.chmod(path, 0o644) if os.path.isfile(path) else None; func(path)
    for dirpath, dirs, files in os.walk(p):
        os.chmod(dirpath, 0o755)
    shutil.rmtree(p, onexc=fix)


def entry_of(p: pathlib.Path) -> pathlib.Path | None:
    """The cache entry folder containing `p`, or None when `p` is outside the cache."""
    try:
        rel = pathlib.Path(p).resolve().relative_to(paths.cache_root().resolve())
    except ValueError:
        return None
    return paths.cache_root() / rel.parts[0] / rel.parts[1] if len(rel.parts) >= 2 else None


def _leases_dir() -> pathlib.Path:
    return paths.state_dir() / 'leases'


def lease(run_id: str, entries: Iterable[pathlib.Path], pid: int | None = None) -> None:
    f = _leases_dir() / f'{run_id}.json'
    with _flock(paths.state_dir() / 'leases.lock'):
        cur = json.loads(f.read_text()) if f.exists() else {'pid': pid or os.getpid(), 'paths': []}
        cur['paths'] = sorted(set(cur['paths']) | {str(e) for e in entries})
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(cur))


def release(run_id: str) -> None:
    with _flock(paths.state_dir() / 'leases.lock'):
        (_leases_dir() / f'{run_id}.json').unlink(missing_ok=True)


def leased() -> set[pathlib.Path]:
    out = set()
    with _flock(paths.state_dir() / 'leases.lock'):
        for f in _leases_dir().glob('*.json'):
            d = json.loads(f.read_text())
            if _alive(d['pid']): out |= {pathlib.Path(p) for p in d['paths']}
            else: f.unlink(missing_ok=True)
    return out


def register_worktree(path: pathlib.Path) -> None:
    d = paths.state_dir() / 'worktrees'
    d.mkdir(parents=True, exist_ok=True)
    (d / hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]).write_text(str(path.resolve()))


def worktrees() -> list[pathlib.Path]:
    d = paths.state_dir() / 'worktrees'
    out = []
    for f in d.glob('*') if d.exists() else ():
        p = pathlib.Path(f.read_text())
        if p.exists(): out.append(p)
        else: f.unlink(missing_ok=True)
    return out


def size_of(p: pathlib.Path) -> int:
    """Bytes on disk (sparse images count what they use)."""
    total = 0
    for dirpath, _, files in os.walk(p):
        for n in files:
            try: total += os.lstat(os.path.join(dirpath, n)).st_blocks * 512
            except FileNotFoundError: pass
    return total


def entries() -> list[pathlib.Path]:
    root = paths.cache_root()
    if not root.exists(): return []
    return [e for kind in root.iterdir() if kind.is_dir() and kind.name not in SKIP_KINDS
            for e in kind.iterdir() if e.is_dir() and not e.name.startswith('.')]


def _parse_bytes(v: str) -> int:
    return int(v[:-1]) << 30 if v[-1:] in 'Gg' else int(v)


def cap_bytes() -> int:
    return _parse_bytes(os.environ.get('NATEMU_TEST_CACHE_CAP', '100G'))


def reserve_bytes() -> int:
    """Free disk the cache leaves on its volume (NATEMU_TEST_DISK_RESERVE, default 10G): on a nearly full disk the
    cache is cleaned below its cap rather than filling the disk."""
    return _parse_bytes(os.environ.get('NATEMU_TEST_DISK_RESERVE', '10G'))


def free_bytes(p: pathlib.Path) -> int:
    return shutil.disk_usage(p).free


def gc(cap_bytes: int, roots: set[pathlib.Path], reserve: int | None = None) -> list[pathlib.Path]:
    """Delete least-recently-used entries that no live lease and no root protects, until the cache is under the cap
    and at least `reserve` bytes (reserve_bytes()) are free on its volume, whichever asks for less cache."""
    es = entries()
    sizes = {e: size_of(e) for e in es}
    total = sum(sizes.values())
    if es:
        reserve = reserve_bytes() if reserve is None else reserve
        cap_bytes = min(cap_bytes, total + free_bytes(paths.cache_root()) - reserve)
    keep = leased() | {pathlib.Path(r) for r in roots}
    deleted = []
    import time as _t
    started = _t.time()
    for e in sorted(es, key=lambda e: e.stat().st_mtime):
        if total <= cap_bytes: break
        if e in keep: continue
        with _flock(paths.cache_root() / 'locks' / f'{e.parent.name}-{e.name}'):
            # re-check under the key's lock: another run may have just used, leased or deleted it
            try:
                if e.stat().st_mtime >= started or e in leased(): continue
            except FileNotFoundError:
                continue
            trash = e.parent / f'.trash-{e.name}-{os.getpid()}'
            try: os.rename(e, trash)
            except FileNotFoundError: continue
            _rmtree(trash)
        total -= sizes[e]; deleted.append(e)
    return deleted
