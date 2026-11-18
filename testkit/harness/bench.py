"""Benchmarks (spec R3.6, R3.7), wrapped: each runs its existing tools/bench script unchanged, alone on the machine, and
reports where its numbers are. Numbers are kept per machine (<run>/bench/<machine>/<case>/) and never compared across
machines. Baseline caching and the drift canary (R3.7) are not part of this wrapper yet.

    [[suite]] kind = "legacy"  harness = "bench"  tier = "bench"
    [suite.cases.<name>]  argv = [...]  needs = ["{raw}/..."]  results = "results.json"
Placeholders: {out} {fexc} {natemu} {runtime} {kernel} {bench_image} {raw}.
"""
import os
import pathlib
import subprocess
import time

from testkit import artifacts, host, paths, resources
from testkit.family import Needs
from testkit.harness import CaseResult, Requirement


class Bench:
    def cases(self, fam, suite):
        return list(suite.extra.get('cases', {}))

    def case_needs(self, fam, suite, case):
        return Needs(exclusive=True, why='measures wall-clock time: nothing else may run')

    def requirements(self, fam, suite, variant):
        return [Requirement('artifact', 'natemu'), Requirement('artifact', 'fexc'), Requirement('resource', 'debian-kernel'),
                Requirement('legacy', 'target'), Requirement('artifact', 'bench-image')]

    def resolve(self, req):
        if req.kind == 'resource': return [resources.fetch(req.name)]
        if req.kind == 'legacy':
            from testkit.harness import legacy
            return legacy.HARNESS.resolve(req)
        return artifacts.resolve(req.name)

    def _subst(self, s, d):
        return s.format(**d)

    def run(self, batch, config):
        from testkit import runloop
        from testkit.sched.pool import raw_root
        n, fexc = artifacts.natemu(), artifacts.fexc()
        h = host.detect()
        out = {}
        for it in batch:
            spec = it.suite.extra['cases'][it.case]
            d = runloop.run_dir(config) / 'bench' / h.machine_id / it.case
            d.mkdir(parents=True, exist_ok=True)
            raw = raw_root()
            vals = {'out': d, 'fexc': fexc, 'natemu': n.stub, 'runtime': n.runtime, 'host': n.host,
                    'kernel': resources.fetch('debian-kernel'), 'raw': raw or '/nonexistent-natemu-raw',
                    'bench_image': artifacts.bench_image() if '{bench_image}' in ' '.join(spec['argv']) else ''}
            missing = [p for p in (self._subst(x, vals) for x in spec.get('needs', [])) if not pathlib.Path(p).exists()]
            if missing:
                out[it.nodeid] = CaseResult('skipped', f'needs {missing[0]} (not on this machine)', 'not-runnable'); continue
            argv = [self._subst(a, vals) for a in spec['argv']]
            env = dict(os.environ, NATEMU_PREBUILT='1', NATEMU_VM_LOCK_HELD='1')
            if raw: env['NATEMU_RAW_ROOT'] = str(raw)
            t0 = time.time()
            with open(d / 'log.txt', 'wb') as f:
                r = subprocess.run(argv, cwd=paths.repo_root(), env=env, stdout=f, stderr=subprocess.STDOUT)
            took = time.time() - t0
            tail = (d / 'log.txt').read_text(errors='replace').strip().splitlines()[-6:]
            if r.returncode == 3:
                out[it.nodeid] = CaseResult('failed', f'contended: another VM ran during the measurement; {d}'); continue
            if r.returncode != 0:
                out[it.nodeid] = CaseResult('failed', f'exit {r.returncode} after {took:.0f} s; log {d / "log.txt"}\n' + '\n'.join(tail))
                continue
            built = ''
            if vals['bench_image']:
                bt = pathlib.Path(vals['bench_image']).parent / 'builder.txt'
                built = f'; image built by {bt.read_text().strip()}' if bt.exists() else ''
            out[it.nodeid] = CaseResult('passed', f'{took:.0f} s on {h.chip}{built}; numbers in {d}', 'measured')
        return out


HARNESS = Bench()
