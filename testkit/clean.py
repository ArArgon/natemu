"""`natemu-test clean` and `natemu-test purge`: free disk by hand.

    clean   this worktree's finished run folders (except kept ones: a `keep` file in the folder), the framework's
            leftover temporary folders, then the cache down to its cap (what every run does when it ends)
    purge   what clean removes, and every cache entry no running run is using: builds, tools, images, inputs, stored
            outputs; Python environments of other requirements.lock versions; the compile cache and build scratch
            too when no run is active on this machine. Kept: the current Python environment and the machine's state
            (scheduler, recorded durations)

Both take --dry-run (say what would go and how much, delete nothing). Running runs are never touched. `purge` refuses
while any other natemu-test process runs on this machine (a run, seeding, xref, a first-time environment set-up, from
any worktree): not everything those use is leased, so only an idle machine can be emptied safely. `clean` then removes
only temporary folders untouched for a day.
"""
import json
import os
import pathlib
import shutil
import tempfile
import time

from testkit import cache, paths, runs

TEMP_PREFIXES = ('testkit-seed-', 'testkit-diff-', 'testkit-xref-')


def _size(p: pathlib.Path) -> int:
    return cache.size_of(p) if p.is_dir() else (p.lstat().st_blocks * 512 if p.exists() else 0)


def _human(n: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB': return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


def other_testkit_processes() -> list[str]:
    """Other natemu-test processes on this machine, from any worktree: "pid command" lines."""
    import subprocess
    me = {os.getpid(), os.getppid()}
    r = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        pid, _, cmd = line.strip().partition(' ')
        if not pid.isdigit() or int(pid) in me: continue
        if '-m testkit' in cmd or '--testkit-worker' in cmd or 'natemu-test' in cmd: out.append(f'{pid} {cmd[:100]}')
    return out


def finished_runs(worktree: pathlib.Path, busy: bool) -> list[pathlib.Path]:
    """This worktree's run folders that no live process owns and that carry no `keep` file. A folder without a readable
    run.json (being written, or an ad-hoc `-m pytest` run's) counts as live while another testkit process runs."""
    d = paths.runs_dir(worktree)
    out = []
    for r in sorted(d.iterdir()) if d.exists() else ():
        if not r.is_dir() or (r / 'keep').exists(): continue
        try: meta = json.loads((r / 'run.json').read_text())
        except (OSError, ValueError):
            if busy: continue
            meta = None
        if meta and not (r / 'result.json').exists() and runs._alive(meta.get('pid', 0)): continue
        out.append(r)
    return out


def temp_leftovers(busy: bool) -> list[pathlib.Path]:
    """The framework's own temporary folders (shared by every worktree); while another testkit process runs, only those
    untouched for a day."""
    t = pathlib.Path(tempfile.gettempdir())
    return [p for p in t.iterdir() if p.name.startswith(TEMP_PREFIXES) and p.is_dir()
            and (not busy or time.time() - p.stat().st_mtime > 86400)]


def _remove(ps, dry: bool) -> int:
    total = 0
    for p in ps:
        total += _size(p)
        if not dry: shutil.rmtree(p, ignore_errors=True)
    return total


def _forget_dead_registry() -> None:
    for f in runs._registry().glob('*.json'):
        try: rec = json.loads(f.read_text())
        except ValueError: continue                     # being written: leave it
        if not runs._alive(rec['pid']) and not pathlib.Path(rec['dir']).exists(): f.unlink(missing_ok=True)


def clean(dry: bool = False, purge: bool = False, all_worktrees: bool = False) -> int:
    others = other_testkit_processes()
    if purge and others and not dry:
        print('purge: other natemu-test processes are running on this machine; purge needs it idle:')
        for o in others[:10]: print('    ' + o)
        return 1
    busy = bool(others)
    worktree = paths.repo_root()
    trees = [worktree] + ([w for w in cache.worktrees() if w.resolve() != worktree.resolve()] if all_worktrees else [])
    rs = [r for w in trees for r in finished_runs(w, busy)]
    ts = temp_leftovers(busy)
    freed_runs, freed_tmp = _remove(rs, dry), _remove(ts, dry)
    verb = 'would free' if dry else 'freed'
    print(f'run folders: {len(rs)} of {len(trees)} worktree{"s" if len(trees) > 1 else ""}, {verb} {_human(freed_runs)}')
    if not all_worktrees:
        others = [w for w in cache.worktrees() if w.resolve() != worktree.resolve()]
        held = sum(_size(r) for w in others for r in finished_runs(w, True))
        if held: print(f'  other worktrees\' finished runs hold {_human(held)} (--all-worktrees cleans them)')
    print(f'temporary folders: {len(ts)}, {verb} {_human(freed_tmp)}'
          + (' (only those untouched for a day: other natemu-test processes are running)' if busy else ''))
    if not dry: _forget_dead_registry()
    if purge:
        if dry and others: print(f'(purge would refuse now: {len(others)} other natemu-test processes running)')
        held = cache.leased()
        es = [e for e in cache.entries() if e not in held]
        sizes = sum(cache.size_of(e) for e in es)
        if not dry: cache.gc(0, set())                 # cap 0, no roots: the machine is idle
        print(f'cache entries: {len(es)}, {verb} {_human(sizes)}')
        if not dry and other_testkit_processes():       # one started meanwhile: leave what it may be using
            print('Python environments, compile cache and build scratch: kept (a natemu-test process started meanwhile)')
        else:
            import hashlib
            tag = hashlib.sha256((worktree / 'testkit' / 'requirements.lock').read_bytes()).hexdigest()[:16]
            vd = paths.cache_root() / 'venv'
            old = [v for v in vd.iterdir() if v.name != tag and not v.name.startswith('.')] if vd.exists() else []
            print(f'Python environments of other requirements.lock versions: {len(old)}, {verb} {_human(_remove(old, dry))}')
            scratch = [paths.cache_root() / k for k in ('compile-cache', 'build-tmp')]
            print(f'compile cache and build scratch: {verb} {_human(_remove([x for x in scratch if x.exists()], dry))}')
    else:
        before = sum(cache.size_of(e) for e in cache.entries())
        if not dry:
            roots = set()
            for f in (paths.state_dir() / 'used').glob('*'):
                rec = json.loads(f.read_text())
                if pathlib.Path(rec['worktree']).exists(): roots |= {pathlib.Path(p) for p in rec['paths']}
            gone = cache.gc(cache.cap_bytes(), roots)
            print(f'cache: {_human(before)}, cap {_human(cache.cap_bytes())}: {len(gone)} entries deleted')
        else:
            print(f'cache: {_human(before)}, cap {_human(cache.cap_bytes())} (purge empties it)')
    return 0
