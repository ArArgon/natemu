"""Runs: foreground (`run`) or detached (`submit`), each with a folder under the worktree's .natemu/runs/ (spec R7.4),
registered machine-wide so `status` can show every run's place in the queue (R7.3)."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import uuid

from testkit import cache, paths

KEEP_RUNS = 20


def new_id() -> str:
    return time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:4]


def _registry() -> pathlib.Path:
    d = paths.state_dir() / 'runs'; d.mkdir(parents=True, exist_ok=True)
    return d


def prune(runs: pathlib.Path) -> None:
    """Keep the newest runs and any run with a `keep` file (R8.5)."""
    if not runs.exists(): return
    ds = sorted((d for d in runs.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)
    for d in ds[KEEP_RUNS:]:
        if not (d / 'keep').exists(): shutil.rmtree(d, ignore_errors=True)


def run(args: list[str], run_id: str | None = None) -> int:
    import pytest
    rid = run_id or new_id()
    root = paths.repo_root()
    d = paths.runs_dir(root) / rid
    d.mkdir(parents=True, exist_ok=True)
    (d / 'run.json').write_text(json.dumps({'id': rid, 'pid': os.getpid(), 'args': args, 'worktree': str(root),
                                            'started': time.time()}))
    (_registry() / f'{rid}.json').write_text(json.dumps({'dir': str(d), 'pid': os.getpid()}))
    cache.register_worktree(root)
    prune(paths.runs_dir(root))
    os.environ['TESTKIT_RUN_DIR'] = str(d)
    argv = ['-c', str(root / 'pytest.ini'), '--rootdir', str(root), f'--junitxml={d / "junit.xml"}', *args]
    try:
        code = int(pytest.main(argv))
    finally:
        cleanup(rid, root)
    (d / 'result.json').write_text(json.dumps({'exit': code, 'finished': time.time()}))
    return code


def cleanup(rid: str, worktree: pathlib.Path) -> None:
    """Record what this run used as the worktree's roots, release its lease, and clean the cache to its cap (R8.4)."""
    lease = paths.state_dir() / 'leases' / f'{rid}.json'
    used = json.loads(lease.read_text())['paths'] if lease.exists() else []
    u = paths.state_dir() / 'used'; u.mkdir(parents=True, exist_ok=True)
    import hashlib
    (u / hashlib.sha256(str(worktree).encode()).hexdigest()[:16]).write_text(json.dumps({'worktree': str(worktree), 'paths': used}))
    cache.release(rid)
    roots = set()
    for f in u.glob('*'):
        rec = json.loads(f.read_text())
        if pathlib.Path(rec['worktree']).exists(): roots |= {pathlib.Path(p) for p in rec['paths']}
        else: f.unlink(missing_ok=True)
    cache.gc(cache.cap_bytes(), roots)


def submit(args: list[str]) -> str:
    rid = new_id()
    d = paths.runs_dir() / rid; d.mkdir(parents=True, exist_ok=True)
    log = open(d / 'run.log', 'wb')
    subprocess.Popen([sys.executable, '-m', 'testkit', 'run', '--run-id', rid, *args], cwd=paths.repo_root(),
                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=dict(os.environ))
    return rid


def _find(rid: str) -> pathlib.Path | None:
    f = _registry() / f'{rid}.json'
    if f.exists(): return pathlib.Path(json.loads(f.read_text())['dir'])
    d = paths.runs_dir() / rid
    return d if d.exists() else None


def _alive(pid: int) -> bool:
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False
    except PermissionError: return True


def describe(rid: str) -> str:
    d = _find(rid)
    if d is None: return f'{rid}: no such run'
    res = d / 'result.json'
    if res.exists(): state = f'finished, exit status {json.loads(res.read_text())["exit"]}'
    elif (d / 'run.json').exists() and _alive(json.loads((d / 'run.json').read_text())['pid']): state = 'running'
    elif (d / 'run.json').exists(): state = 'ended without a result (killed?)'
    else: state = 'starting'
    tail = ''
    if (d / 'run.log').exists():
        lines = (d / 'run.log').read_text(errors='replace').splitlines()
        tail = '\n'.join('    ' + l for l in lines[-5:])
    return f'{rid}: {state} ({d})' + ('\n' + tail if tail else '')


def status(rid: str | None) -> str:
    if rid: return describe(rid)
    from testkit.sched.pool import Pool
    p = Pool()
    st = p.snapshot()
    out = [f'machine: {p.cfg.p_cores} performance + {p.cfg.e_cores} efficiency cores; window budget {p.cfg.window_budget:.0f} s']
    for g in st.held:
        r = g.request
        out.append(f'  running  {r.label:40} {g.core_class}{r.cores:<2} {r.worktree:20} {time.time() - g.start:6.0f} s')
    for r in sorted(st.waiting, key=lambda r: (-r.priority, r.asked)):
        out.append(f'  waiting  {r.label:40} {"X " if r.exclusive else str(r.cores) + " "} {r.worktree:20} {p.waiting_reason(r.id)}')
    active = []
    for f in _registry().glob('*.json'):
        rec = json.loads(f.read_text())
        if _alive(rec['pid']): active.append(f.stem)
    out.append('runs: ' + (', '.join(sorted(active)) if active else 'none active'))
    return '\n'.join(out)


def wait(rid: str) -> int:
    d = _find(rid)
    if d is None: print(f'{rid}: no such run'); return 2
    while not (d / 'result.json').exists():
        if (d / 'run.json').exists() and not _alive(json.loads((d / 'run.json').read_text())['pid']):
            print(f'{rid}: ended without a result'); return 3
        time.sleep(1)
    print(describe(rid))
    return json.loads((d / 'result.json').read_text())['exit']
