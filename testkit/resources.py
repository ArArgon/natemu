"""External resources (spec R14): declared once in tests/resources.toml with mirrors and a pinned sha256, fetched into
the cache once per machine, never used unverified; and resources derived from them by recipes in the repo."""
import hashlib
import inspect
import pathlib
import subprocess
import tarfile
import tomllib
import urllib.request
from typing import Callable

from testkit import cache, paths

AGENT = 'natemu-testkit'        # generic: no user or project details in download requests (R14.8)


class Unpreparable(Exception):
    def __init__(self, name: str, reason: str):
        super().__init__(f'{name}: {reason}'); self.name, self.reason = name, reason


def declarations() -> dict:
    f = paths.repo_root() / 'tests' / 'resources.toml'
    return tomllib.loads(f.read_text()).get('resource', {}) if f.exists() else {}


def _download(url: str, dest: pathlib.Path) -> None:
    req = urllib.request.Request(url, headers={'User-Agent': AGENT})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, 'wb') as f:
        while chunk := r.read(1 << 20): f.write(chunk)


def _extract(archive: pathlib.Path, member: str, out: pathlib.Path) -> None:
    """`member` from a .deb (ar holding data.tar.*) or a tar archive, to out/<basename>."""
    if archive.read_bytes()[:8] == b'!<arch>\n':
        tmp = out / '.deb'; tmp.mkdir()
        subprocess.run(['ar', 'x', archive], cwd=tmp, check=True)
        archive = next(tmp.glob('data.tar*'))
    with tarfile.open(archive) as t:
        m = next((x for x in t.getmembers() if x.name.lstrip('./') == member.lstrip('./')), None)
        if m is None: raise FileNotFoundError(f'{member} not in archive')
        (out / pathlib.PurePosixPath(member).name).write_bytes(t.extractfile(m).read())


def fetch(name: str, decl: dict | None = None) -> pathlib.Path:
    """The verified file (or, with `extract`, the extracted member) for resource `name`."""
    d = decl if decl is not None else declarations().get(name)
    if d is None: raise Unpreparable(name, 'not declared in tests/resources.toml')
    pin = d['sha256']
    errors = []

    def build(out: pathlib.Path):
        f = out / ('download' if d.get('extract') else pathlib.PurePosixPath(d['urls'][0]).name or 'download')
        for url in d['urls']:
            try:
                _download(url, f)
            except Exception as e:      # any network or HTTP error: try the next mirror
                errors.append(f'{url}: {e}'); continue
            got = hashlib.sha256(f.read_bytes()).hexdigest()
            if got != pin:
                errors.append(f'{url}: sha256 {got}, pinned {pin}'); f.unlink(); continue
            if d.get('extract'):
                _extract(f, d['extract'], out); f.unlink()
            return
        raise Unpreparable(name, '; '.join(errors))

    entry = cache.get_or_build('resource', cache.key(name, pin, d.get('extract', '')), build)
    files = [p for p in entry.iterdir() if not p.name.startswith('.')]
    return files[0]


def derived(name: str, recipe: Callable[[pathlib.Path, list], None], inputs: list) -> pathlib.Path:
    """A resource built by `recipe(out_dir, inputs)`; keyed by the recipe's source and its inputs (files or strings)."""
    k = cache.key(name, inspect.getsource(recipe), *inputs)
    return cache.get_or_build('derived', k, lambda out: recipe(out, inputs))
