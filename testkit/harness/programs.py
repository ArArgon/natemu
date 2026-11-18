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
import shutil
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

    @staticmethod
    def reference_of(suite, name: str) -> str:
        """The case's reference: an exact override, else the first matching glob override, else the suite's (R2.13a)."""
        import fnmatch
        ov = suite.extra.get('reference_overrides', {})
        if name in ov: return ov[name]
        return next((r for pat, r in ov.items() if fnmatch.fnmatchcase(name, pat)), suite.reference)

    def seed(self, fam, suite, rules=None) -> dict:
        work = pathlib.Path(tempfile.mkdtemp(prefix='testkit-seed-'))
        try: return self._seed(fam, suite, work)
        finally: shutil.rmtree(work, ignore_errors=True)

    def _seed(self, fam, suite, work) -> dict:
        """Answers from each case's reference: `qemu` (the pinned QEMU user build, through ref.run_programs; a program may
        name a `reference_program` twin that QEMU runs instead, as gen_br66 does), `x86` (tools/reference/ref_native.py on
        NATEMU_X86_HOST; suite `x86_flag` is the CPU flag it must have) or `fex` (natemu's own result: the reference for x87
        while it runs in FEX's reduced precision). Flow programs also lock QEMU's RFLAGS mask."""
        from testkit import family as fammod
        ref, _ = _ref()
        _, programs = build_input(fam, suite.input)
        os.environ.setdefault('NATEMU_REFERENCE_BACKEND', 'apple')
        os.environ['NATEMU_REF_CACHE'] = 'off'
        by_ref = {}
        for p in programs: by_ref.setdefault(self.reference_of(suite, p['name']), []).append(p)
        answers, masks, tools = {}, {}, set()
        if q := by_ref.get('qemu'):
            runs = [dict(p['reference_program'], name=p['name']) if 'reference_program' in p else p for p in q]
            results, prov = ref.run_programs(runs, work / 'qemu', artifacts.qemu_user())
            tools.add(f'qemu: qemu-x86_64-static ({prov["container_backend"]})')
            for p, r in zip(q, results):
                m = ref.num(r['rflags_compare_mask']) if ref.norm(p)['flow'] else None
                if m is not None: masks[p['name']] = hex(m)
                answers[p['name']] = answer(p, r['qemu'], m)
        if x86 := by_ref.get('x86'):
            hostname = os.environ.get('NATEMU_X86_HOST')
            if hostname:
                pf = work / 'x86.json'; pf.write_text(json.dumps({'programs': x86}))
                flag = ['--require-flag', suite.extra['x86_flag']] if suite.extra.get('x86_flag') else []
                subprocess.run([sys.executable, str(REF / 'ref_native.py'), pf, '-o', work / 'x86-out.json', '--host', hostname,
                                *flag], check=True)
                for p, r in zip(x86, json.loads((work / 'x86-out.json').read_text())['results']):
                    answers[p['name']] = answer(p, r['qemu'])
                tools.add(f'x86: {hostname}')
            else:
                for p in x86: answers[p['name']] = ''          # no answer yet: these cases report stale-lock
        if mp := by_ref.get('model'):
            # tools/models/afp_fp: the Intel SDM's FP rules (fp.BASE); it reproduces every QEMU answer of the i8 corpora and the
            # real-x86 answers where AMD and Intel agree. The reference where they may not (two-NaN choices).
            md = str(paths.repo_root() / 'tools' / 'models' / 'afp_fp')
            if md not in sys.path: sys.path.insert(0, md)
            import fp as fpmodel, predict
            for p in mp:
                answers[p['name']] = answer(p, predict.model_result(p, fpmodel.BASE)['qemu'])
            tools.add('model: tools/models/afp_fp (Intel SDM rules)')
        if fexp := by_ref.get('fex'):
            n, fexc = artifacts.natemu(), artifacts.fexc()
            for p in fexp:
                vm = self._run_natemu(n, fexc, p, dict(os.environ), work / 'fex' / p['name'], None)
                answers[p['name']] = answer(p, vm) if isinstance(vm, dict) and vm.get('stop') == 'reached' else ''
            tools.add(f'fex: natemu {n.key[:12]} (FEX reduced-precision x87)')
        excluded = {}
        if fam.rules:
            for v in fammod.expand(suite):
                met = {k[5:]: ('real' if v.get(k) else 'hidden') for k, _ in v.params if k.startswith('host.')}
                ex = sorted(p['name'] for p in programs if fam.rules(suite.name, p['name'], v, met))
                if ex: excluded[v.id] = ex
        h = host.detect()
        return {'fingerprint': lock.fingerprint_suite(fam, suite, sources(), qemu_pin()), 'reference': suite.reference,
                'input': corpus_hash(programs),
                'seeded': {'tool': f'{suite.reference}: ' + '; '.join(sorted(tools)), 'pin': qemu_pin(), 'machine': h.chip,
                           'date': datetime.date.today().isoformat()},
                'cases': {p['name']: answers[p['name']] for p in programs}, 'excluded': excluded,
                'x86_cases': sorted(p['name'] for p in by_ref.get('x86', [])),
                'fex_cases': sorted(p['name'] for p in by_ref.get('fex', [])),
                'model_cases': sorted(p['name'] for p in by_ref.get('model', [])), 'masks': masks}

    @staticmethod
    def _run_natemu(n, fexc, p, env, work, max_block):
        """One program in natemu (`run-program`): the driver's report, or an error string."""
        import run_slice1_vm as v1
        work.mkdir(parents=True, exist_ok=True)
        (work / 'program.txt').write_text(v1.description(p))
        r = subprocess.run([n.host, n.runtime, 'run-program', fexc, work / 'program.txt', work,
                            *([str(max_block)] if max_block else []), *v1.CODE_SHAPE], capture_output=True, text=True, env=env)
        if r.returncode != 0: return f'natemu-host exit {r.returncode}: {r.stderr[-1500:]}'
        return json.loads(r.stdout)

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
                if (cpl := it.variant.get('program.cpl')) is not None:
                    p = dict(p, cpl=cpl)            # run at this CPL against the same answer (the programs are CPL-neutral)
                vm = self._run_natemu(n, fexc, p, e, pathlib.Path(td) / it.case, it.variant.get('engine.max_block') or None)
                if isinstance(vm, str):
                    out[it.nodeid] = CaseResult('failed', vm); continue
                if limit := v1.declared_limit(vm):
                    out[it.nodeid] = CaseResult('skipped', f'stopped at {vm["rip"]}: {limit}', 'unsupported'); continue
                problems = [] if vm['stop'] == 'reached' else [f"stopped {vm['stop']} {vm['reason']} at {vm['rip']}"]
                problems += v1.page_checks(p, vm)
                m = locked.get('masks', {}).get(it.case)
                got = normalize(p, vm, int(m, 16) if m else None)
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
        with tempfile.TemporaryDirectory(prefix='testkit-diff-') as td:
            res, _ = ref.run_programs([p], td, artifacts.qemu_user())
        return '; '.join(ref.compare(res[0], [ref.num(x) for x in vm['gpr']], ref.num(vm['rflags']), ref.num(vm['rip']),
                                     vm['memory'])) or 'no difference under ref.compare'


HARNESS = Programs()
