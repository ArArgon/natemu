"""Preparation before any test starts (spec R14.4): every artifact, resource and input the selected units need is built
or fetched once, under one shared CPU claim (never inside an exclusive window), and leased for the run (R8.4)."""
import os
import time
import uuid

from testkit import cache
from testkit.resources import Unpreparable
from testkit.sched.policy import Request


REMOTE_KINDS = ('campaign-image',)      # requirements built wholly in a container (on the x86 machine when configured)


class BuildFailure(Exception):
    def __init__(self, name: str, reason: str):
        super().__init__(f'{name}: {reason}'); self.name, self.reason = name, reason


def prepare(items, pool, config, record: dict | None = None) -> dict:
    """nodeid -> (category, reason) for items whose requirements could not be prepared. `record` (testkit.timings)
    gets each requirement's time, whether it was built or fetched, and the wait for the CPU claim."""
    rec = record if record is not None else {}
    rec.setdefault('reqs', []); rec.setdefault('waited', 0.0)
    needs = {}                  # requirement -> (harness, [items])
    for it in items:
        if not hasattr(it.harness, 'requirements'): continue
        for r in it.harness.requirements(it.fam, it.suite, it.variant):
            needs.setdefault(r, (it.harness, []))[1].append(it)
    if not needs: return {}
    from testkit import runloop
    run_id = runloop.run_dir(config).name
    tr = config.pluginmanager.get_plugin('terminalreporter')
    failed = {}

    def attempt(r, harness, its, built=False):
        t0, ok = time.time(), False
        try:
            entries = harness.resolve(r)
            cache.lease(run_id, [cache.entry_of(e) for e in entries if cache.entry_of(e)])
            ok = True
        except Unpreparable as e:
            for it in its: failed[it.nodeid] = ('unpreparable', str(e))
        except BuildFailure as e:
            for it in its: failed[it.nodeid] = ('build-failure', str(e))
        except cache.Miss:
            raise
        except Exception as e:      # anything else (a tool missing, a subprocess error) fails this requirement only,
            # by its message alone: a traceback would list each frame's arguments, the build's environment among them
            for it in its: failed[it.nodeid] = ('build-failure', f'{r.kind} {r.name}: {type(e).__name__}: {e}')
        rec['reqs'].append({'kind': r.kind, 'name': str(r.name), 's': time.time() - t0, 'built': built, 'ok': ok})

    # First pass, no claim: everything already in the cache is only looked up (and leased).
    missing = []
    with cache.peek():
        for r, (harness, its) in needs.items():
            try: attempt(r, harness, its)
            except cache.Miss: missing.append(r)
    if not missing: return failed
    # Something must be built or fetched: that takes a shared CPU claim, never inside an exclusive window (R14.4).
    from testkit import artifacts
    remote = artifacts.container_builder() != 'local'
    # Campaign images built on the x86 machine only wait on it and copy the image back: a light claim (see
    # testkit.sched.policy) that blocks nothing here, so exclusive windows and other runs go on meanwhile. The rest is
    # built here: one core when the other container builds are remote, else four.
    away = [r for r in missing if remote and r.kind in REMOTE_KINDS]
    here = [r for r in missing if r not in away]
    for group, cores, light in ((here, 1 if remote else min(4, pool.cfg.p_cores), False), (away, 1, True)):
        if not group: continue
        req = Request(id=f"{run_id}:{uuid.uuid4().hex}", owner_pid=os.getpid(), cores=cores, label="prepare",
                      worktree=str(config.rootpath.name), run=run_id, light=light)
        pool.submit(req)
        asked = time.time()
        said = None
        while not pool.poll(req.id):
            w = pool.waiting_reason(req.id)
            if w != said and tr: tr.write_line(f'testkit: preparing waits: {w}'); said = w
            time.sleep(0.2)
        rec['waited'] += time.time() - asked
        try:
            for r in group:
                if tr: tr.write_line(f'testkit: preparing {r.kind} {r.name}' + (' (on the x86 builder)' if light else ''))
                attempt(r, *needs[r], built=True)
        finally:
            pool.release(req.id)
    return failed
