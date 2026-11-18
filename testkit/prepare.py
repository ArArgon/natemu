"""Preparation before any test starts (spec R14.4): every artifact, resource and input the selected units need is built
or fetched once, under one shared CPU claim (never inside an exclusive window), and leased for the run (R8.4)."""
import os
import time
import uuid

from testkit import cache
from testkit.resources import Unpreparable
from testkit.sched.policy import Request


class BuildFailure(Exception):
    def __init__(self, name: str, reason: str):
        super().__init__(f'{name}: {reason}'); self.name, self.reason = name, reason


def prepare(items, pool, config) -> dict:
    """nodeid -> (category, reason) for items whose requirements could not be prepared."""
    needs = {}                  # requirement -> (harness, [items])
    for it in items:
        if not hasattr(it.harness, 'requirements'): continue
        for r in it.harness.requirements(it.fam, it.suite, it.variant):
            needs.setdefault(r, (it.harness, []))[1].append(it)
    if not needs: return {}
    from testkit import runloop
    run_id = runloop.run_dir(config).name
    req = Request(id=uuid.uuid4().hex, owner_pid=os.getpid(), cores=min(4, pool.cfg.p_cores), label='prepare',
                  worktree=str(config.rootpath.name))
    pool.submit(req)
    tr = config.pluginmanager.get_plugin('terminalreporter')
    said = None
    while not pool.poll(req.id):
        r = pool.waiting_reason(req.id)
        if r != said and tr: tr.write_line(f'testkit: preparing waits: {r}'); said = r
        time.sleep(0.2)
    failed = {}
    try:
        for r, (harness, its) in needs.items():
            if tr: tr.write_line(f'testkit: preparing {r.kind} {r.name}')
            try:
                entries = harness.resolve(r)
                cache.lease(run_id, [cache.entry_of(e) for e in entries if cache.entry_of(e)])
            except Unpreparable as e:
                for it in its: failed[it.nodeid] = ('unpreparable', str(e))
            except BuildFailure as e:
                for it in its: failed[it.nodeid] = ('build-failure', str(e))
    finally:
        pool.release(req.id)
    return failed
