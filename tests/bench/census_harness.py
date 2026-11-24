"""The bench family's harness for the computed-exit census (tests/bench/run_census.py; docs/restructure/el0-sampler-plan.md,
chunk 2): QEMU with the census plugin. Counts, not timings, so unlike testkit's bench harness a case does not take the
whole machine: it claims its `needs` (cores for QEMU's threads), and another VM running meanwhile does not matter.

    [[suite]] kind = "bench" | "judged"  harness = "census_harness.py:Census"  [tier = "bench"]
    [suite.cases.<name>]  argv = [...]  needs = { cores = N, ... }  timeout = N

Placeholders (in argv): {out} (the case's folder: <run>/bench/<machine>/<case>/ for a bench suite, kept; <run>/out/<case>
for a judged one, removed when it passes), {qemu_plugins} (the plugin QEMU's qemu-system-x86_64, artifacts.qemu_plugins),
{census_plugin} (the plugin, artifacts.census_plugin), {kernel} (resource debian-kernel), {bench_image}
(artifacts.bench_image) and {io5_inputs} (a writable clone of inputs.io5_inputs()), each prepared before the run when
a case uses it. A case passes when its command exits 0; a bench suite's pass is `measured`, its numbers in its folder.

Loaded by testkit into its own process (testkit.family.harness_for), never run."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import os
import shutil
import subprocess
import time

from testkit import artifacts, host, inputs, paths, resources
from testkit.family import Needs
from testkit.harness import CaseResult, Requirement


def _uses(spec, name) -> bool:
    return '{' + name + '}' in ' '.join(spec['argv'])


class Census:
    def cases(self, fam, suite):
        return list(suite.extra.get('cases', {}))

    def _spec(self, suite, case):
        return suite.extra['cases'][case]

    def case_timeout(self, fam, suite, case):
        return self._spec(suite, case).get('timeout')

    def case_needs(self, fam, suite, case, variant=None):
        n = dict(self._spec(suite, case).get('needs', {}))
        return Needs(**n) if n else suite.needs

    def requirements(self, fam, suite, variant):
        reqs = [Requirement('artifact', 'qemu-plugins'), Requirement('artifact', 'census-plugin')]
        for spec in suite.extra.get('cases', {}).values():
            if _uses(spec, 'kernel'): reqs.append(Requirement('resource', 'debian-kernel'))
            if _uses(spec, 'bench_image'): reqs.append(Requirement('artifact', 'bench-image'))
            if _uses(spec, 'io5_inputs'): reqs.append(Requirement('input', 'io5-inputs'))
        return list(dict.fromkeys(reqs))

    def resolve(self, req):
        if req.kind == 'resource': return [resources.fetch(req.name)]
        if req.kind == 'input': return [inputs.io5_inputs()]
        return artifacts.resolve(req.name)

    def _values(self, spec, d: pathlib.Path) -> dict:
        vals = {'out': d, 'qemu_plugins': artifacts.qemu_plugins() / 'bin' / 'qemu-system-x86_64',
                'census_plugin': artifacts.census_plugin()}
        if _uses(spec, 'kernel'): vals['kernel'] = resources.fetch('debian-kernel')
        if _uses(spec, 'bench_image'): vals['bench_image'] = artifacts.bench_image()
        if _uses(spec, 'io5_inputs'):
            dest = d / 'inputs-src' / 'io5-inputs'
            if dest.exists(): shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            inputs.writable_clone(inputs.io5_inputs(), dest)    # run_io5.py run clones disk.raw keeping its mode
            vals['io5_inputs'] = dest
        return vals

    def run(self, batch, config):
        from testkit import runloop
        h = host.detect()
        out = {}
        for it in batch:
            spec = self._spec(it.suite, it.case)
            bench = it.suite.kind == 'bench'
            d = runloop.run_dir(config) / ('bench' if bench else 'out') / (h.machine_id if bench else '') / it.case
            if d.exists() and not bench: shutil.rmtree(d)
            d.mkdir(parents=True, exist_ok=True)
            vals = self._values(spec, d)
            argv = [a.format(**vals) for a in spec['argv']]
            if argv[0].endswith('.py'): argv = [sys.executable, *argv]
            env = dict(os.environ, NATEMU_PREBUILT='1', NATEMU_VM_LOCK_HELD='1')
            t0 = time.time()
            log = d / 'log.txt'
            with open(log, 'wb') as f:
                r = subprocess.run(argv, cwd=paths.repo_root(), env=env, stdout=f, stderr=subprocess.STDOUT)
            took = time.time() - t0
            tail = '\n'.join(log.read_text(errors='replace').strip().splitlines()[-8:])
            if r.returncode != 0:
                out[it.nodeid] = CaseResult('failed', f'exit {r.returncode} after {took:.0f} s; log {log}\n{tail}')
            elif bench:
                out[it.nodeid] = CaseResult('passed', f'{took:.0f} s on {h.chip}; counts in {d}', 'measured')
            else:
                out[it.nodeid] = CaseResult('passed', f'{took:.0f} s')
                if os.environ.get('TESTKIT_KEEP_OUTPUTS') != '1': shutil.rmtree(d, ignore_errors=True)
        return out
