"""How many times as long CPU work takes on this Mac's efficiency cores than on its performance cores (the scheduling
rules' `e_slowdown`, testkit.sched.policy rule 5): measured once per machine and macOS build, then cached in the state
folder (e_slowdown.json; delete it to measure again).

The measurement runs one CPU loop unconfined (a performance core, on an idle machine) and one as the framework confines
efficiency-core work (`taskpolicy -c background`, testkit/runloop.py) at the same time: the median ratio of three 2 s
rounds, 6 s once per machine. The efficiency cores are shared with macOS's own background work, so single rounds vary:
on the M2 Max twelve 2 s rounds read 2.9x to 4.3x (median 3.4x), a median of three 3.0x to 3.6x in 80 % of draws;
tools/probe_cores.py's 8 s rounds read 3.1x and units recorded on both classes ran 2.4x to 3.5x as long. Rule 5 is
not sensitive to that spread (replays at 2.4x and 3x placed alike). It runs only when the framework has nothing running here and the load is low; otherwise DEFAULT is used for now and the
measurement waits for an idle start. NATEMU_TEST_E_SLOWDOWN overrides it (1 turns rule 5 off).
"""
import contextlib
import fcntl
import json
import os
import pathlib
import statistics
import subprocess
import sys
import time

DEFAULT = 3.0               # until measured: the M2 Max's measured 3.1x, rounded (tools/probe_cores.py)
LIMITS = (1.0, 10.0)        # a measured value outside is clamped
TRUST = 1.5                 # a measurement below is not cached: both loops on efficiency cores (the framework itself
                            # started under a background clamp) or on busy cores; DEFAULT is used and it measures again
ROUNDS, SECONDS = 3, 2.0
FILE = 'e_slowdown.json'
LOOP = r'''
import hashlib, sys, time
t = time.perf_counter() + float(sys.argv[1]); n = 0; b = b'x' * 4096
while time.perf_counter() < t:
    for _ in range(200): b = hashlib.sha256(b).digest() * 128
    n += 1
print(n)
'''


def measure(rounds: int = ROUNDS, seconds: float = SECONDS) -> tuple[float, dict]:
    """(slowdown, details): the median over `rounds` of the performance-core loop rate over the efficiency-core loop
    rate, the two loops running at once."""
    rounds_ = []
    for _ in range(rounds):
        procs = [subprocess.Popen(prefix + [sys.executable, '-c', LOOP, str(seconds)], stdout=subprocess.PIPE, text=True)
                 for prefix in ([], ['taskpolicy', '-c', 'background'])]
        p, e = (int(x.communicate(timeout=60)[0]) for x in procs)
        rounds_.append((p / max(1, e), p / seconds, e / seconds))
    raw, p_rate, e_rate = statistics.median_low(rounds_)
    return min(max(raw, LIMITS[0]), LIMITS[1]), {'p_rate': p_rate, 'e_rate': e_rate, 'raw': raw,
                                                  'rounds': [round(r[0], 2) for r in rounds_]}


@contextlib.contextmanager
def _locked(d: pathlib.Path):
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / 'e_slowdown.lock', os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)              # one measurer at a time: two at once would slow each other
    try: yield
    finally: fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


def cached(state_dir: pathlib.Path, host) -> float | None:
    """The cached measurement when it is this machine's under this macOS build (and a usable value)."""
    try:
        c = json.loads((state_dir / FILE).read_text())
        v = float(c['value'])
    except (OSError, ValueError, KeyError, TypeError): return None
    ok = c.get('machine_id') == host.machine_id and c.get('os_build') == host.os_build and LIMITS[0] <= v <= LIMITS[1]
    return v if ok else None


def override() -> float | None:
    v = os.environ.get('NATEMU_TEST_E_SLOWDOWN')
    if not v: return None
    try: f = float(v)
    except ValueError: f = 0.0
    if not f >= 1: raise ValueError(f'NATEMU_TEST_E_SLOWDOWN={v!r}: a number of at least 1 (1 turns rule 5 off)')
    return f


def e_slowdown(state_dir: pathlib.Path, host, idle) -> float:
    """This machine's efficiency-core slowdown: the override, the cached measurement, or (when `idle()` says the machine
    is free to measure) a new one, cached; else DEFAULT."""
    if (v := override()) is not None: return v
    if host.e_cores == 0: return 1.0
    if (v := cached(state_dir, host)) is not None: return v
    if not idle(): return DEFAULT
    with _locked(state_dir):
        if (v := cached(state_dir, host)) is not None: return v      # another process measured while this one waited
        if _failed_recently(state_dir, host): return DEFAULT
        try: value, details = measure()
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            _write(state_dir, {**_key(host), 'value': None, 'failed': f'{type(e).__name__}: {e}', 'at': time.time()})
            return DEFAULT
        if not idle(): return DEFAULT                                # work started meanwhile: measured again later
        if details['raw'] < TRUST:
            _write(state_dir, {**_key(host), 'value': None, 'failed': f'implausible reading {details["raw"]:.2f}',
                               'at': time.time()})
            return DEFAULT
        rec = {'machine_id': host.machine_id, 'chip': host.chip, 'os_build': host.os_build, 'value': value,
               'measured': time.strftime('%Y-%m-%dT%H:%M:%S'), **details}
        _write(state_dir, rec)
        return value


RETRY = 3600.0              # s after a failed or implausible measurement before measuring again (DEFAULT meanwhile)


def _key(host) -> dict:
    return {'machine_id': host.machine_id, 'chip': host.chip, 'os_build': host.os_build}


def _write(state_dir: pathlib.Path, rec: dict) -> None:
    tmp = state_dir / f'{FILE}.{os.getpid()}'
    tmp.write_text(json.dumps(rec)); os.replace(tmp, state_dir / FILE)


def _failed_recently(state_dir: pathlib.Path, host) -> bool:
    """A failed measurement of this machine and build less than RETRY ago: not every start spends 6 s on it again."""
    try:
        c = json.loads((state_dir / FILE).read_text())
        at = float(c.get('at') or 0)
    except (OSError, ValueError, TypeError, AttributeError): return False
    return (c.get('failed') is not None and c.get('machine_id') == host.machine_id and c.get('os_build') == host.os_build
            and time.time() - at < RETRY)
