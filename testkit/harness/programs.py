"""Straight-line x86 program corpora (spec R2): a generator builds the corpus on any machine, seeding runs it under the
pinned QEMU (or real x86) once and locks one answer hash per program, and every run compares natemu's normalized final
state with that hash. Nothing reads natemu-raw.

The normalized state is exactly what tools/reference/ref.py:compare compares: the 16 GPRs, RFLAGS under the mask its
rules derive from the program's own annotations (flags.analyze), RIP, and the program's memory regions.
"""
import datetime
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile

from testkit import artifacts, cache, host, lock, paths
from testkit.harness import CaseResult, Requirement
from testkit.prepare import BuildFailure

REF = paths.repo_root() / 'tools' / 'reference'
for p in (REF, paths.repo_root() / 'tools'):
    if str(p) not in sys.path: sys.path.insert(0, str(p))


from testkit.harness.normalize_programs import _ref, answer, corpus_hash, mask_of, normalize  # noqa: E402,F401


def build_input(fam, name: str) -> tuple[pathlib.Path, list]:
    d = fam.inputs[name]
    k = lock.fingerprint_input(fam, name)

    def build(out):
        r = subprocess.run([sys.executable, str(paths.repo_root() / d.generator), '-o', str(out / 'programs.json'), *d.args],
                           cwd=paths.repo_root(), capture_output=True, text=True,
                           env=dict(os.environ, NATEMU_ASM_CACHE='off'))
        if r.returncode != 0: raise BuildFailure(f'input {name}', (r.stderr or r.stdout)[-1500:])
    entry = cache.get_or_build('input', k, build)
    return entry, json.loads((entry / 'programs.json').read_text())['programs']


def qemu_pin() -> str:
    ref, _ = _ref()
    return ref.PINS['qemu-x86_64-static']


def sources() -> list[pathlib.Path]:
    """What decides an answer besides the input (R2.7): this normalizer and the reference tool's sources."""
    from testkit.harness import normalize_programs
    return [pathlib.Path(normalize_programs.__file__), REF / 'ref.py', REF / 'flags.py', REF / 'container_entry.py']


class Programs:
    def lock_status(self, fam, suite):
        return lock.status(fam, suite, sources(), qemu_pin())

    def cases(self, fam, suite):
        return list(lock.read(fam).get('suite', {}).get(suite.name, {}).get('cases', {}))

    def locked_exclusions(self, fam, suite):
        return lock.read(fam).get('suite', {}).get(suite.name, {}).get('excluded', {})

    def requirements(self, fam, suite, variant):
        return [Requirement('artifact', 'natemu'), Requirement('artifact', 'fexc'), Requirement('input', f'{fam.dir}|{suite.input}')]

    def resolve(self, req):
        if req.kind == 'input':
            from testkit import family
            d, name = req.name.split('|')
            return [build_input(family.load(pathlib.Path(d) / 'family.toml'), name)[0]]
        return artifacts.resolve(req.name)

    # --- seeding (natemu-test lock) ---

    def seed(self, fam, suite, rules=None) -> dict:
        from testkit import family as fammod
        ref, _ = _ref()
        _, programs = build_input(fam, suite.input)
        overrides = suite.extra.get('reference_overrides', {})
        work = pathlib.Path(tempfile.mkdtemp(prefix='testkit-seed-'))
        os.environ.setdefault('NATEMU_REFERENCE_BACKEND', 'apple')
        os.environ['NATEMU_REF_CACHE'] = 'off'
        qemu_progs = [p for p in programs if overrides.get(p['name'], suite.reference) == 'qemu']
        results, prov = ref.run_programs(qemu_progs, work / 'qemu', artifacts.qemu_user())
        answers = {p['name']: answer(p, r['qemu']) for p, r in zip(qemu_progs, results)}
        x86 = [p for p in programs if overrides.get(p['name'], suite.reference) == 'x86']
        if x86:
            hostname = os.environ.get('NATEMU_X86_HOST')
            if hostname:
                pf = work / 'x86.json'; pf.write_text(json.dumps({'programs': x86}))
                subprocess.run([sys.executable, str(REF / 'ref_native.py'), pf, '-o', work / 'x86-out.json', '--host', hostname],
                               check=True)
                for p, r in zip(x86, json.loads((work / 'x86-out.json').read_text())['results']):
                    answers[p['name']] = answer(p, r['qemu'])
            else:
                for p in x86: answers[p['name']] = ''          # no answer yet: these cases report stale-lock
        excluded = {}
        if fam.rules:
            for v in fammod.expand(suite):
                met = {k[5:]: ('real' if v.get(k) else 'hidden') for k, _ in v.params if k.startswith('host.')}
                ex = sorted(p['name'] for p in programs if fam.rules(suite.name, p['name'], v, met))
                if ex: excluded[v.id] = ex
        h = host.detect()
        return {'fingerprint': lock.fingerprint_suite(fam, suite, sources(), qemu_pin()), 'reference': suite.reference,
                'input': corpus_hash(programs),
                'seeded': {'tool': f'{suite.reference}: qemu-x86_64-static ({prov["container_backend"]})' if suite.reference == 'qemu'
                           else 'x86', 'pin': qemu_pin(), 'machine': h.chip,
                           'date': datetime.date.today().isoformat()},
                'cases': {p['name']: answers[p['name']] for p in programs}, 'excluded': excluded}

    # --- running ---

    def run(self, batch, config) -> dict:
        import run_slice1_vm as v1
        fam, suite = batch[0].fam, batch[0].suite
        entry, programs = build_input(fam, suite.input)
        locked = lock.read(fam)['suite'][suite.name]
        if corpus_hash(programs) != locked.get('input'):
            msg = f'input {suite.input} built here does not match the lock (local tools: {self._tools()})'
            return {it.nodeid: CaseResult('skipped', msg, 'build-failure') for it in batch}
        by = {p['name']: p for p in programs}
        n = artifacts.natemu(); fexc = artifacts.fexc()
        env = dict(os.environ, NATEMU_COMPILE_CACHE=str(artifacts.compile_cache(n)))
        out = {}
        with tempfile.TemporaryDirectory() as td:
            for it in batch:
                p = by.get(it.case)
                if p is None:
                    out[it.nodeid] = CaseResult('skipped', f'{it.case} is not in the built input', 'stale-lock'); continue
                want = locked['cases'].get(it.case)
                if not want:
                    out[it.nodeid] = CaseResult('skipped', 'no answer yet (needs its reference)', 'stale-lock'); continue
                fp = it.variant.get('engine.fp')
                e = dict(env, **({'NATEMU_FP': fp} if fp else {}))
                key = self._reuse_key(n, locked, it)
                if not config.getoption('fresh') and (hit := self._reused(key)):
                    ok = hashlib.sha256(hit).hexdigest() == want
                    out[it.nodeid] = CaseResult('passed' if ok else 'failed', '' if ok else 'reused output differs from the lock',
                                                'reused' if ok else None); continue
                work = pathlib.Path(td) / it.case
                work.mkdir()
                (work / 'program.txt').write_text(v1.description(p))
                r = subprocess.run([n.host, n.runtime, 'run-program', fexc, work / 'program.txt', work, *v1.CODE_SHAPE],
                                   capture_output=True, text=True, env=e)
                if r.returncode != 0:
                    out[it.nodeid] = CaseResult('failed', f'natemu-host exit {r.returncode}: {r.stderr[-1500:]}'); continue
                vm = json.loads(r.stdout)
                if limit := v1.declared_limit(vm):
                    out[it.nodeid] = CaseResult('skipped', f'stopped at {vm["rip"]}: {limit}', 'unsupported'); continue
                problems = [] if vm['stop'] == 'reached' else [f"stopped {vm['stop']} {vm['reason']} at {vm['rip']}"]
                problems += v1.page_checks(p, vm)
                got = normalize(p, vm)
                if hashlib.sha256(got).hexdigest() != want:
                    problems.append('final state differs from the locked answer'
                                    + ('' if not config.getoption('diff') else ': ' + self._diff(p, vm)))
                if problems:
                    out[it.nodeid] = CaseResult('failed', '; '.join(problems))
                else:
                    self._store(key, got)
                    out[it.nodeid] = CaseResult('passed', output=got)
        return out

    @staticmethod
    def _tools():
        r = subprocess.run(['clang', '--version'], capture_output=True, text=True)
        return r.stdout.splitlines()[0] if r.stdout else 'clang missing'

    @staticmethod
    def _reuse_key(n, locked, it) -> str:
        h = host.detect()
        return cache.key('outputs', n.key, artifacts.fexc().parent.name, locked.get('input', ''), it.case, it.variant.id,
                         h.chip, json.dumps(it.place.met), h.os_build, *sources(), pathlib.Path(__file__),
                         paths.repo_root() / 'tools' / 'run_slice1_vm.py')

    @staticmethod
    def _reused(key) -> bytes | None:
        f = cache.entry_path('outputs', key) / 'state.json'
        return f.read_bytes() if f.exists() else None

    @staticmethod
    def _store(key, data: bytes) -> None:
        cache.get_or_build('outputs', key, lambda out: (out / 'state.json').write_bytes(data))

    @staticmethod
    def _diff(p, vm) -> str:
        """Seed this one case to show what differs (needs the QEMU reference: R2.4)."""
        ref, _ = _ref()
        res, _ = ref.run_programs([p], tempfile.mkdtemp(prefix='testkit-diff-'), artifacts.qemu_user())
        return '; '.join(ref.compare(res[0], [ref.num(x) for x in vm['gpr']], ref.num(vm['rflags']), ref.num(vm['rip']),
                                     vm['memory'])) or 'no difference under ref.compare'


HARNESS = Programs()
