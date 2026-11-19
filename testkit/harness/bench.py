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
    {fio_prep} {block_inputs} {io5_inputs}   writable clones, in the case folder, of inputs.fio_prep(),
                        inputs.block_inputs() and inputs.io5_inputs() (APFS clones: no space until written)
    {qemu_img}          the pinned QEMU build's qemu-img (artifacts.qemu_system)
    {resume_dir}        a results folder kept across runs, the same while the case and what it names are (resume_dir())
"""
import os
import pathlib
import shutil
import subprocess
import time

from testkit import artifacts, cache, host, inputs, paths, resources
from testkit.family import Needs
from testkit.harness import CaseResult, Requirement

INPUTS = {'fio_prep': 'fio-prep', 'block_inputs': 'block-inputs', 'io5_inputs': 'io5-inputs'}     # placeholder -> input name
INPUT_FNS = {'fio-prep': lambda: inputs.fio_prep(), 'block-inputs': lambda: inputs.block_inputs(),
             'io5-inputs': lambda: inputs.io5_inputs()}
# Given as writable clones: run_io_matrix.py prepare writes into the folders it copies from fio_prep and block_inputs;
# run_io5.py run clones disk.raw (keeping its mode) and grows the clone to 16 GiB.
WRITABLE = {'fio_prep', 'block_inputs', 'io5_inputs'}


def _text(spec) -> str:
    return ' '.join([*spec['argv'], *spec.get('env', {}).values(), *spec.get('needs', [])])


def _uses(spec, name) -> bool:
    return '{' + name + '}' in _text(spec)


# The scripts a resumable case runs: their contents key the resume folder too (a changed job list, profile or metric
# must not mix with old records or meet inputs prepared by the old script).
RESUME_SCRIPTS = ('tools/run_m1_perf.py', 'tools/run_io_matrix.py', 'tools/run_fio_admission.py', 'tools/run_fio_baselines.py',
                  'tools/run_io5.py', 'tools/io_common.py', 'tools/run_virtio_block.py')
RESUME_KEEP_DAYS = 14         # `natemu-test clean` removes resume folders untouched this long; purge removes all


def resume_dir(case: str, spec: dict, ident: dict, machine: str) -> pathlib.Path:
    """A results folder that outlives the run, for a case whose script resumes from its own records (run_m1_perf.py
    full --dir): the same on every run of the case on this machine while the case's command line and everything its
    placeholders name (builds and inputs, by their cache entries, which are content-addressed) stay the same, so a rerun
    finishes what is left instead of repeating finished measurements. Anything new (a new final build, a changed input
    or command line) gets a new folder, and so does a change to the scripts it runs (RESUME_SCRIPTS). In the machine's
    state folder; `natemu-test clean` removes folders untouched for RESUME_KEEP_DAYS, purge all of them."""
    scripts = [paths.repo_root() / s for s in RESUME_SCRIPTS]
    k = cache.key('bench-resume-v2', case, machine, *spec['argv'], *(f'{a}={b}' for a, b in sorted(spec.get('env', {}).items())),
                  *(f'{a}={b}' for a, b in sorted(ident.items())), *(p for p in scripts if p.exists()))
    return paths.state_dir() / 'bench-resume' / case / k[:32]


class Bench:
    def cases(self, fam, suite):
        return list(suite.extra.get('cases', {}))

    def case_needs(self, fam, suite, case):
        return Needs(exclusive=True, why='measures wall-clock time: nothing else may run')

    def requirements(self, fam, suite, variant):
        reqs = [Requirement('artifact', 'natemu'), Requirement('artifact', 'fexc'), Requirement('resource', 'debian-kernel'),
                Requirement('legacy', 'target')]
        for spec in suite.extra.get('cases', {}).values():
            if _uses(spec, 'bench_image'): reqs.append(Requirement('artifact', 'bench-image'))
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

    def _values(self, spec, d, n, fexc, raw, case='', machine='', fresh=False) -> dict:
        vals = {'out': d, 'fexc': fexc, 'natemu': n.stub, 'runtime': n.runtime, 'host': n.host,
                'kernel': resources.fetch('debian-kernel'), 'raw': raw or '/nonexistent-natemu-raw',
                'bench_image': artifacts.bench_image() if _uses(spec, 'bench_image') else ''}
        if _uses(spec, 'natemu_entry'): vals['natemu_entry'] = n.entry
        if _uses(spec, 'natemu_baseline'): vals['natemu_baseline'] = artifacts.natemu_at(spec['baseline']).entry
        if _uses(spec, 'qemu_img'): vals['qemu_img'] = artifacts.qemu_system() / 'qemu-img'
        ident = {k: str(v) for k, v in vals.items() if k not in ('out', 'raw') and _uses(spec, k)}
        for p, name in INPUTS.items():
            if not _uses(spec, p): continue
            src = INPUT_FNS[name]()
            ident[p] = str(src)                      # the cache entry, not this run's clone of it
            if p in WRITABLE:
                dest = d / 'inputs-src' / name
                if dest.exists(): shutil.rmtree(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                inputs.writable_clone(src, dest)
                src = dest
            vals[p] = src
        if _uses(spec, 'resume_dir'):
            r = vals['resume_dir'] = resume_dir(case, spec, ident, machine)
            if fresh and r.exists():                 # --fresh: measure everything again; the old records are set aside
                r.rename(r.with_name(f'{r.name}.set-aside-{int(time.time())}'))
            r.mkdir(parents=True, exist_ok=True)
            os.utime(r)                              # in use: clean's age counts from here
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
            vals = self._values(spec, d, n, fexc, raw, it.case, h.machine_id, bool(config and config.getoption('fresh')))
            where = f'{d}' + (f' (results in {vals["resume_dir"]}, kept for resuming)' if 'resume_dir' in vals else '')
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
                out[it.nodeid] = CaseResult('failed', f'exit {r.returncode} after {took:.0f} s; log {d / "log.txt"}; {where}\n' + '\n'.join(tail))
                continue
            built = ''
            if vals['bench_image']:
                bt = pathlib.Path(vals['bench_image']).parent / 'builder.txt'
                built = f'; image built by {bt.read_text().strip()}' if bt.exists() else ''
            out[it.nodeid] = CaseResult('passed', f'{took:.0f} s on {h.chip}{built}; numbers in {where}', 'measured')
        return out


HARNESS = Bench()
