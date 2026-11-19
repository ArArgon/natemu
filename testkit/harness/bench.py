"""Benchmarks (spec R3.6, R3.7), wrapped: each runs its existing tools/bench script unchanged, alone on the machine, and
reports where its numbers are. Numbers are kept per machine (<run>/bench/<machine>/<case>/) and never compared across
machines. Baseline caching and the drift canary (R3.7) are not part of this wrapper yet.

    [[suite]] kind = "legacy"  harness = "bench"  tier = "bench"  params = { timeout = SECONDS }
    [suite.cases.<name>]  argv = [...]  needs = ["{raw}/..."]  env = { NAME = "..." }  baseline = "REV"
Placeholders (in argv, env and needs): {out} {fexc} {natemu} {runtime} {host} {kernel} {raw}, and these, each built only
when a case of the suite uses it (and then prepared before the run starts):
    {bench_image}       the benchmark initramfs (artifacts.bench_image)
    {natemu_entry}      this checkout's natemu build folder, laid out like cargo's target folder (artifacts.natemu)
    {natemu_baseline}   the same for the case's `baseline` revision (artifacts.natemu_at)
    {fio_prep} {block_inputs}   writable clones, in the case folder, of inputs.fio_prep() / inputs.block_inputs()
    {io5_inputs}        inputs.io5_inputs()
    {qemu_img}          the pinned QEMU build's qemu-img (artifacts.qemu_system)
"""
import os
import pathlib
import shutil
import subprocess
import time

from testkit import artifacts, host, inputs, paths, resources
from testkit.family import Needs
from testkit.harness import CaseResult, Requirement

INPUTS = {'fio_prep': 'fio-prep', 'block_inputs': 'block-inputs', 'io5_inputs': 'io5-inputs'}     # placeholder -> input name
INPUT_FNS = {'fio-prep': lambda: inputs.fio_prep(), 'block-inputs': lambda: inputs.block_inputs(),
             'io5-inputs': lambda: inputs.io5_inputs()}
WRITABLE = {'fio_prep', 'block_inputs'}         # run_io_matrix.py prepare writes into the folders it copies from these


def _text(spec) -> str:
    return ' '.join([*spec['argv'], *spec.get('env', {}).values(), *spec.get('needs', [])])


def _uses(spec, name) -> bool:
    return '{' + name + '}' in _text(spec)


class Bench:
    def cases(self, fam, suite):
        return list(suite.extra.get('cases', {}))

    def case_needs(self, fam, suite, case):
        return Needs(exclusive=True, why='measures wall-clock time: nothing else may run')

    def requirements(self, fam, suite, variant):
        reqs = [Requirement('artifact', 'natemu'), Requirement('artifact', 'fexc'), Requirement('resource', 'debian-kernel'),
                Requirement('legacy', 'target'), Requirement('artifact', 'bench-image')]
        for spec in suite.extra.get('cases', {}).values():
            if _uses(spec, 'natemu_baseline'): reqs.append(Requirement('natemu-at', spec['baseline']))
            reqs += [Requirement('input', n) for p, n in INPUTS.items() if _uses(spec, p)]
            if _uses(spec, 'qemu_img'): reqs.append(Requirement('artifact', 'qemu-system'))
        return list(dict.fromkeys(reqs))

    def resolve(self, req):
        if req.kind == 'resource': return [resources.fetch(req.name)]
        if req.kind == 'legacy':
            from testkit.harness import legacy
            return legacy.HARNESS.resolve(req)
        if req.kind == 'natemu-at': return [artifacts.natemu_at(req.name).entry]
        if req.kind == 'input': return [INPUT_FNS[req.name]()]
        return artifacts.resolve(req.name)

    def _subst(self, s, d):
        return s.format(**d)

    def _values(self, spec, d, n, fexc, raw) -> dict:
        vals = {'out': d, 'fexc': fexc, 'natemu': n.stub, 'runtime': n.runtime, 'host': n.host,
                'kernel': resources.fetch('debian-kernel'), 'raw': raw or '/nonexistent-natemu-raw',
                'bench_image': artifacts.bench_image() if _uses(spec, 'bench_image') else ''}
        if _uses(spec, 'natemu_entry'): vals['natemu_entry'] = n.entry
        if _uses(spec, 'natemu_baseline'): vals['natemu_baseline'] = artifacts.natemu_at(spec['baseline']).entry
        if _uses(spec, 'qemu_img'): vals['qemu_img'] = artifacts.qemu_system() / 'qemu-img'
        for p, name in INPUTS.items():
            if not _uses(spec, p): continue
            src = INPUT_FNS[name]()
            if p in WRITABLE:
                dest = d / 'inputs-src' / name
                if dest.exists(): shutil.rmtree(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                inputs.writable_clone(src, dest)
                src = dest
            vals[p] = src
        return vals

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
            vals = self._values(spec, d, n, fexc, raw)
            missing = [p for p in (self._subst(x, vals) for x in spec.get('needs', [])) if not pathlib.Path(p).exists()]
            if missing:
                out[it.nodeid] = CaseResult('skipped', f'needs {missing[0]} (not on this machine)', 'not-runnable'); continue
            argv = [self._subst(a, vals) for a in spec['argv']]
            env = dict(os.environ, NATEMU_PREBUILT='1', NATEMU_VM_LOCK_HELD='1',
                       **{k: self._subst(v, vals) for k, v in spec.get('env', {}).items()})
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
