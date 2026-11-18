"""The machine's content-addressed cache (spec R8): entries `<root>/<kind>/<key>/`, built once machine-wide, published
only when complete, protected by per-run leases and by roots while in use, and cleaned least-recently-used first."""
import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
from typing import Callable, Iterable

from testkit import paths

SKIP_KINDS = {'state', 'locks', 'venv'}     # not entries: machine state, build locks, the framework's own environments


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


def get_or_build(kind: str, k: str, build: Callable[[pathlib.Path], None], *, size_hint: int = 0) -> pathlib.Path:
    """The entry, building it with `build(tmpdir)` if missing. One builder per key machine-wide; a waiter gets its result.
    A failed or killed build leaves nothing that is ever served."""
    entry = entry_path(kind, k)
    if entry.is_dir():
        os.utime(entry); return entry
    with _flock(paths.cache_root() / 'locks' / f'{kind}-{k}'):
        if entry.is_dir():
            os.utime(entry); return entry
        for old in entry.parent.glob(f'.tmp-{k}-*'):        # a killed builder's leftovers (we hold the key's lock)
            shutil.rmtree(old, ignore_errors=True)
        tmp = entry.parent / f'.tmp-{k}-{os.getpid()}'
        tmp.mkdir(parents=True)
        try:
            build(tmp)
            os.rename(tmp, entry)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True); raise
    return entry


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


def cap_bytes() -> int:
    v = os.environ.get('NATEMU_TEST_CACHE_CAP', '100G')
    return int(v[:-1]) << 30 if v[-1:] in 'Gg' else int(v)


def gc(cap_bytes: int, roots: set[pathlib.Path]) -> list[pathlib.Path]:
    """Delete least-recently-used entries that no live lease and no root protects, until the cache is under the cap."""
    es = entries()
    sizes = {e: size_of(e) for e in es}
    total = sum(sizes.values())
    keep = leased() | {pathlib.Path(r) for r in roots}
    deleted = []
    for e in sorted(es, key=lambda e: e.stat().st_mtime):
        if total <= cap_bytes: break
        if e in keep: continue
        with _flock(paths.cache_root() / 'locks' / f'{e.parent.name}-{e.name}'):
            trash = e.parent / f'.trash-{e.name}-{os.getpid()}'
            os.rename(e, trash)
            shutil.rmtree(trash, ignore_errors=True)
        total -= sizes[e]; deleted.append(e)
    return deleted
