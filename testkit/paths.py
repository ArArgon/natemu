"""Where testkit keeps things. The cache is per machine and shared by every worktree; run outputs stay with the worktree."""
import os
import pathlib

_REPO = pathlib.Path(__file__).resolve().parent.parent


def repo_root() -> pathlib.Path:
    return _REPO


def cache_root() -> pathlib.Path:
    v = os.environ.get('NATEMU_TEST_CACHE')
    return pathlib.Path(v) if v else pathlib.Path.home() / 'Library/Caches/natemu/testkit'


def state_dir() -> pathlib.Path:
    return cache_root() / 'state'


def runs_dir(worktree: pathlib.Path | None = None) -> pathlib.Path:
    return (worktree or repo_root()) / '.natemu' / 'runs'
