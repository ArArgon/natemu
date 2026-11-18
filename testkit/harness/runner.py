"""Runner scripts as migrated families (spec R13.2): a case runs one tools/run_*.py invocation, unchanged in what it
checks, with its inputs from the framework's input root (testkit.inputs; NATEMU_RAW_ROOT points there, never at
natemu-raw), its writable state in the machine's state folder (NATEMU_STATE_ROOT), and its QEMU answers in locked mode
(NATEMU_ANSWERS=locked, the family's tests.lock): it never starts QEMU. Each case reports per case where the runner
writes per-case results (`adapters` below); a runner that writes none is judged by its exit status alone, and says so.

    [[suite]] kind = "judged"  harness = "runner"
    [suite.cases.<name>]  argv = ["tools/run_x.py", "--fexc", "{fexc}", "--out", "{out}", ...]
                          needs = {...}  smoke = true  judge = "judges:name"  results = "results.json"
"""
import fnmatch
import json
import os
import pathlib
import subprocess
import sys
import time

from testkit import artifacts, paths
from testkit.family import Needs
from testkit.harness import CaseResult, Requirement, SubResult
from testkit.harness import legacy


def _state_root() -> pathlib.Path:
    d = paths.state_dir() / 'runner-state'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _input_root():
    try:
        from testkit import inputs
    except ImportError:
        return None
    return inputs.root()


# --- per-case adapters: each turns one results shape into [(name, outcome, message)] or returns None ---

def _shape_results_list(d):
    if isinstance(d, dict) and isinstance(d.get('results'), list) and d['results'] and 'name' in d['results'][0]:
        out = []
        for r in d['results']:
            o = r.get('outcome') or ('match' if r.get('ok') else 'fail')
            out.append((r['name'], 'passed' if o in ('match', 'ok', 'pass', 'deviated', 'excluded') else
                        'skipped' if o in ('unsupported', 'skip') else 'failed',
                        '; '.join(map(str, r.get('diffs') or r.get('problems') or r.get('errors') or []))[:2000]))
        return out
    return None


def _shape_top_list(d):
    if isinstance(d, list) and d and isinstance(d[0], dict) and 'name' in d[0]:
        out = []
        for r in d:
            bad = r.get('problems') or r.get('errors') or ([] if r.get('ok', True) else ['not ok'])
            out.append((r['name'], 'failed' if bad else 'passed', '; '.join(map(str, bad))[:2000]))
        return out
    return None


def _shape_pass_map(d):
    if isinstance(d, dict) and isinstance(d.get('pass'), (list, dict)):
        p = d['pass']
        if isinstance(p, dict):
            return [(k, 'passed' if v else 'failed', '; '.join(d.get('reasons', {}).get(k, []))) for k, v in p.items()]
    return None


ADAPTERS = (_shape_results_list, _shape_top_list, _shape_pass_map)


def per_case(out_dir: pathlib.Path, name: str) -> list | None:
    f = out_dir / name
    if not f.exists(): return None
    try: d = json.loads(f.read_text())
    except ValueError: return None
    for a in ADAPTERS:
        if (r := a(d)) is not None: return r
    return None


def legacy_raw():
    from testkit.sched.pool import raw_root
    return raw_root() or '/nonexistent-natemu-raw'


class Runner:
    def cases(self, fam, suite):
        return list(suite.extra.get('cases', {}))

    def _spec(self, suite, case):
        return suite.extra['cases'][case]

    def case_needs(self, fam, suite, case):
        n = dict(self._spec(suite, case).get('needs', {}))
        return Needs(**n) if n else suite.needs

    def is_smoke(self, case):
        return False

    def requirements(self, fam, suite, variant):
        reqs = [Requirement('artifact', 'natemu'), Requirement('artifact', 'fexc'), Requirement('legacy', 'target'),
                Requirement('artifact', 'qemu-system')]
        return reqs + [Requirement('input-root', 'root')]

    def resolve(self, req):
        if req.kind == 'input-root':
            r = _input_root()
            return [r] if r else []
        return legacy.HARNESS.resolve(req)

    def run(self, batch, config):
        from testkit import runloop
        out = {}
        n, fexc = artifacts.natemu(), artifacts.fexc()
        rundir = runloop.run_dir(config)
        root = _input_root()
        for it in batch:
            spec = self._spec(it.suite, it.case)
            o = rundir / 'out' / it.case
            argv = [a.format(fexc=fexc, out=o, natemu_entry=n.entry, raw=root or legacy_raw()) for a in spec['argv']]
            if argv[0].endswith('.py'): argv = [sys.executable, *argv]
            env = dict(os.environ, NATEMU_PREBUILT='1', NATEMU_VM_LOCK_HELD='1', NATEMU_STATE_ROOT=str(_state_root()),
                       NATEMU_ANSWERS='locked', NATEMU_ANSWERS_LOCK=str(it.fam.dir / 'tests.lock'),
                       NATEMU_REFERENCE_BACKEND='apple', NATEMU_COMPILE_CACHE=str(artifacts.compile_cache(n)),
                       **{k: str(v) for k, v in spec.get('env', {}).items()})
            if root: env['NATEMU_RAW_ROOT'] = str(root)
            qbin = artifacts.qemu_system()           # the pinned QEMU 11.1.2 build (system mode and qemu-img)
            env.update(NATEMU_QEMU_SYSTEM=str(qbin / 'qemu-system-x86_64'), NATEMU_QEMU_IMG=str(qbin / 'qemu-img'),
                       NATEMU_QEMU_PIN='qemu-system 11.1.2 src', PATH=f'{qbin}:{env.get("PATH", "")}')
            builder = spec.get('builds', False)
            attempts = 2 if spec.get('rerun', it.suite.rerun) else 1
            notes, ok = [], False
            for k in range(attempts):
                with legacy.build_lock(builder):
                    ok, note = self.once(argv, env, o, rundir, it.case, k, spec, it)
                    if builder: legacy.install_signed(n)
                notes.append(note)
                if ok: break
            subs = per_case(o, spec.get('results', 'results.json'))
            if subs is None:
                notes.append('no per-case results')
            res = CaseResult('passed' if ok else 'failed', ' | '.join(notes),
                             sub=[SubResult(name, oc, msg) for name, oc, msg in (subs or [])])
            out[it.nodeid] = res
        return out

    def once(self, argv, env, o, rundir, case, attempt, spec, it):
        log = rundir / 'runner' / (case + ('.rerun' if attempt else '') + '.log')
        log.parent.mkdir(parents=True, exist_ok=True)
        if attempt and o.exists(): o.rename(o.with_name(o.name + '.first'))
        t0 = time.time()
        with open(log, 'wb') as f:
            r = subprocess.run(argv, cwd=paths.repo_root(), env=env, stdout=f, stderr=subprocess.STDOUT)
        took = f'{time.time() - t0:.0f} s'
        text = log.read_text(errors='replace')
        if 'ANSWER-MISSING' in text:
            return False, f'answers not seeded for this runner (ANSWER-MISSING); log {log}'
        if r.returncode == 0: return True, f'exit 0 ({took})'
        if r.returncode == 1 and spec.get('judge'):
            from testkit import family as fammod
            mod, fn = spec['judge'].split(':')
            j = getattr(fammod._load_module(it.fam.dir / f'{mod}.py', f'testkit_judges_{it.fam.name}'), fn)
            ok, why = j(o)
            return ok, (f'judged: {why} ({took})' if ok else f'judge: {why}; log {log}')
        return False, f'exit {r.returncode} after {took}; log {log}'


HARNESS = Runner()
