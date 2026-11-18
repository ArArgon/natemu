"""Recorded durations per item (spec R6.5), per machine: longest-first ordering and the backfill rule use them."""
import contextlib
import fcntl
import json
import os

from testkit import paths

ALPHA = 0.3


@contextlib.contextmanager
def _locked():
    d = paths.state_dir(); d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / 'durations.lock', os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        f = d / 'durations.json'
        data = json.loads(f.read_text()) if f.exists() else {}
        yield data
        tmp = d / f'durations.json.{os.getpid()}'
        tmp.write_text(json.dumps(data)); os.replace(tmp, f)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


def load() -> dict:
    f = paths.state_dir() / 'durations.json'
    return json.loads(f.read_text()) if f.exists() else {}


def expected(nodeid: str, table: dict | None = None) -> float | None:
    return (table if table is not None else load()).get(nodeid)


def record(nodeid: str, seconds: float) -> None:
    with _locked() as d:
        old = d.get(nodeid)
        d[nodeid] = seconds if old is None else old + ALPHA * (seconds - old)


def record_many(items: dict) -> None:
    with _locked() as d:
        for nodeid, seconds in items.items():
            old = d.get(nodeid)
            d[nodeid] = seconds if old is None else old + ALPHA * (seconds - old)
