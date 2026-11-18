"""Wrapped run_suite steps (spec R13.1): every step of tools/run_suite.py not yet moved to a family runs unchanged, with
its own pass rule (exit status, or the step's judge), its claims taken from run_suite's tables, and every result
labelled `wrapped`. Wrapped steps still use natemu-raw and live QEMU; that is what migrating a family removes.

Builds stay out of the cache: the worktree's target/ gets the signed binaries of the natemu artifact before any
wrapped step runs, and the steps that build for themselves (l1, l2, l2-iasl, boot-smoke) hold the worktree's build
lock exclusively, after which the signed binaries are copied back.
"""
import contextlib
import fcntl
import fnmatch
import functools
import os
import pathlib
import shutil
import subprocess
import sys
import time

from testkit import artifacts, paths
from testkit.family import Needs
from testkit.harness import CaseResult, Requirement

TOOLS = paths.repo_root() / 'tools'
BUILDS = {'build', 'cargo-test', 'cargo-test-fexc', 'runtime', 'release', 'pytests', 'split-wait-model'}   # the unit family
BUILDERS = {'l1', 'l2', 'l2-iasl', 'boot-smoke'}
LAYOUT = ('release/natemu-host', 'release/natemu', 'aarch64-unknown-none/release/natemu-runtime')


@functools.cache
def _suite_module():
    if str(TOOLS) not in sys.path: sys.path.insert(0, str(TOOLS))
    import run_suite
    return run_suite


def steps(fexc='FEXC', out=pathlib.Path('/OUT')):
    return {s.name: s for s in _suite_module().steps(str(fexc), pathlib.Path(out))}


def target_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get('CARGO_TARGET_DIR', paths.repo_root() / 'target'))


def install_signed(n) -> None:
    """Put the artifact's signed binaries where the old runners look: the worktree's target/ (clones, no copy cost)."""
    t = target_dir()
    for rel in LAYOUT:
        dst = t / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + f'.testkit-{os.getpid()}')
        subprocess.run(['cp', '-c', str(n.entry / rel), str(tmp)], check=True)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dst)


@contextlib.contextmanager
def build_lock(exclusive: bool):
    f = paths.repo_root() / '.natemu' / 'legacy-build.lock'
    f.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(f, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH); yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


class Legacy:
    def cases(self, fam, suite):
        replaced = set(suite.params.get('replaced', []))
        return [n for n in steps() if n not in BUILDS and n not in replaced]

    def case_needs(self, fam, suite, case) -> Needs:
        rs = _suite_module()
        s = steps()[case]
        if s.phase == 'ser' or case in BUILDERS:
            why = rs.SERIAL_WHY.get(case, 'builds natemu in the worktree (run_suite pre phase)')
            return Needs(exclusive=True, why=why)
        return Needs(cores=max(1, s.w), perf=not any(fnmatch.fnmatch(case, p) for p in rs.E_OK))

    def is_smoke(self, case) -> bool:
        return any(fnmatch.fnmatch(case, p) for p in _suite_module().QUICK)

    def expected(self, case):
        return float(steps()[case].est)

    def requirements(self, fam, suite, variant):
        return [Requirement('artifact', 'natemu'), Requirement('artifact', 'fexc'), Requirement('legacy', 'target')]

    def resolve(self, req):
        if req.kind == 'legacy':
            install_signed(artifacts.natemu()); return []
        return artifacts.resolve(req.name)

    def not_runnable(self, case) -> str | None:
        from testkit.sched.pool import raw_root
        if raw_root() is None: return 'needs natemu-raw (a wrapped step)'
        missing = [a for a in steps()[case].argv if a.startswith(str(raw_root())) and not pathlib.Path(a).exists()]
        return f'needs natemu-raw inputs: {missing[0]}' if missing else None

    def run(self, batch, config):
        from testkit import runloop
        out = {}
        n, fexc = artifacts.natemu(), artifacts.fexc()
        rundir = runloop.run_dir(config)
        for it in batch:
            if why := self.not_runnable(it.case):
                out[it.nodeid] = CaseResult('skipped', why, 'not-runnable'); continue
            s = steps(fexc, rundir)[it.case]
            builder = it.case in BUILDERS
            attempts = 2 if s.phase == 'ser' else 1
            notes = []
            for k in range(attempts):
                with build_lock(builder):
                    ok, note = self.once(s, rundir, k)
                    if builder: install_signed(n)
                notes.append(note)
                if ok: break
            out[it.nodeid] = CaseResult('passed' if ok else 'failed', ' | '.join(notes), 'wrapped')
        return out

    def once(self, s, rundir, attempt):
        log = rundir / 'legacy' / (s.name + (f'.rerun' if attempt else '') + '.log')
        log.parent.mkdir(parents=True, exist_ok=True)
        if attempt:                                     # keep the first attempt's output beside the rerun's (R8.5)
            first = rundir / 'out' / s.name
            if first.exists(): first.rename(first.with_name(first.name + '.first'))
        env = dict(os.environ, NATEMU_PREBUILT='1', NATEMU_VM_LOCK_HELD='1')
        if s.name in ('cargo-test', 'cargo-test-fexc'): env['NATEMU_REQUIRE_QEMU_IMG'] = '1'
        t0 = time.time()
        with open(log, 'wb') as f:
            r = subprocess.run(s.argv, cwd=paths.repo_root(), env=env, stdout=f, stderr=subprocess.STDOUT)
        took = f'{time.time() - t0:.0f} s'
        if r.returncode == 0: return True, f'exit 0 ({took})'
        if r.returncode == 1 and s.judge:
            ok, why = s.judge(rundir / 'out' / s.name)
            if ok: return True, f'judged: {why} ({took})'
            return False, f'judge: {why}; log {log}'
        return False, f'exit {r.returncode} after {took}; log {log}'


HARNESS = Legacy()
