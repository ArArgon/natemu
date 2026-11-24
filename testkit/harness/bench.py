"""Benchmarks (spec R3.6, R3.7): each runs its tools/bench script (or tests/bench/run_m1_perf.py) unchanged, alone on the machine, and
reports where its numbers are. Numbers are kept per machine (<run>/bench/<machine>/<case>/) and never compared across
machines. Baseline caching and the drift canary (R3.7) are not part of this wrapper yet.

    [[suite]] kind = "bench"  harness = "bench"  tier = "bench"  params = { timeout = SECONDS }
    [suite.cases.<name>]  argv = [...]  needs = ["{raw}/..."]  env = { NAME = "..." }  baseline = "REV"  timeout = N
                          remote = true  (its VM runs on the x86 machine: no exclusive window; runs only when seeding,
                          TESTKIT_SEED_ANSWERS=1 NATEMU_X86_HOST=user@host, and is skipped otherwise)
Placeholders (in argv, env and needs): {out} {fexc} {natemu} {runtime} {host} {kernel} {raw}, and these, each built only
when a case of the suite uses it (and then prepared before the run starts):
    {bench_image}       the benchmark initramfs (artifacts.bench_image)
    {runtime_perf}      the resident built with its `perf` feature (artifacts.perf_runtime), for profile cases
    {runtime_profile}   the resident built with its `perf` and `profile` features (artifacts.profile_runtime), for
                        sampling cases (the EL0 sampler: `--sample-us`)
    {sample_us}         the sampling cases' interval in us: SAMPLE_US, or TESTKIT_SAMPLE_US for one run
    {natemu_entry}      this checkout's natemu build folder, laid out like cargo's target folder (artifacts.natemu)
    {natemu_baseline}   the same for the case's `baseline` revision (artifacts.natemu_at)
    {fio_prep} {block_inputs} {io5_inputs}   writable clones, in the case folder, of inputs.fio_prep(),
                        inputs.block_inputs() and inputs.io5_inputs() (APFS clones: no space until written)
    {paper_inputs}      inputs.paper_inputs(), read-only: the Paper and Mojang jars and the guest's Debian packages
    {display_inputs}    inputs.display_inputs(), read-only: the display cases' kernel and initramfs with kms_pattern
    {kernel_symbols}    inputs.kernel_symbols(), read-only: the booted kernel's text symbols (the EL0 sampler's report)
    {qemu_system}       the pinned qemu-system-x86_64 binary
    {qemu_img}          the pinned QEMU build's qemu-img (artifacts.qemu_system)
    {resume_dir}        a results folder kept across runs, the same while the case and what it names are (resume_dir())

Benchmarks keep the machine they have always measured: a case that starts the `natemu` CLI (its argv names {natemu} or
{natemu_entry}, this run's build, or {natemu_baseline}, the case's baseline) gets the platform devices off (pvpanic,
SMBIOS, the 8042: PLATFORM_OFF) as its runner's `--natemu-set KEY=VALUE` (natemu's `--set`), or `--baseline-set` for the
baseline build: each device whose key that build's source tree has (platform_off()). Cases that start natemu-host ({host}) need
nothing: its devices are off unless asked for.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

from testkit import artifacts, cache, host, inputs, paths, resources
from testkit.family import Needs
from testkit.harness import CaseResult, Requirement, runner

INPUTS = {'fio_prep': 'fio-prep', 'block_inputs': 'block-inputs', 'io5_inputs': 'io5-inputs',   # placeholder -> input name
          'paper_inputs': 'paper-inputs', 'display_inputs': 'display-inputs',
          'kernel_symbols': 'kernel-symbols'}
INPUT_FNS = {'fio-prep': lambda: inputs.fio_prep(), 'block-inputs': lambda: inputs.block_inputs(),
             'io5-inputs': lambda: inputs.io5_inputs(), 'paper-inputs': lambda: inputs.paper_inputs(),
             'display-inputs': lambda: inputs.display_inputs(), 'kernel-symbols': lambda: inputs.kernel_symbols()}
# Given as writable clones: run_io_matrix.py prepare writes into the folders it copies from fio_prep and block_inputs;
# run_io5.py run clones disk.raw (keeping its mode) and grows the clone to 16 GiB.
WRITABLE = {'fio_prep', 'block_inputs', 'io5_inputs'}


def _text(spec) -> str:
    return ' '.join([*spec['argv'], *spec.get('env', {}).values(), *spec.get('needs', [])])


def _uses(spec, name) -> bool:
    return '{' + name + '}' in _text(spec)


# The scripts a resumable case runs: their contents key the resume folder too (a changed job list, profile or metric
# must not mix with old records or meet inputs prepared by the old script).
RESUME_SCRIPTS = ('tests/bench/run_m1_perf.py', 'tests/io/run_io_matrix.py', 'tests/io/run_fio_admission.py', 'tests/io/run_fio_baselines.py',
                  'tests/io/run_io5.py', 'tests/lib/io_common.py', 'tests/io/run_virtio_block.py')
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


_PIECES: dict = {}


def _piece_count(spec) -> int:
    """The runs the case's script plans (`plan --count` with the case's --sets), once per process."""
    argv = spec['argv']
    sets = argv[argv.index('--sets') + 1] if '--sets' in argv else None
    script = next(a for a in argv if a.endswith('.py'))
    k = (script, sets)
    if k not in _PIECES:
        r = subprocess.run([sys.executable, str(paths.repo_root() / script), 'plan', '--count', *(['--sets', sets] if sets else [])],
                           capture_output=True, text=True)
        try: _PIECES[k] = int(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError): _PIECES[k] = 0
        if r.returncode != 0 or _PIECES[k] <= 0:       # loud: never fall back to one unit of the whole case
            raise RuntimeError(f'{script} plan --count gave no piece count (exit {r.returncode}): {(r.stderr or r.stdout)[-300:]}')
    return _PIECES[k]


def _session(path: pathlib.Path) -> dict:
    """A pieced case's state in this run: when it started, whether --fresh was applied, how many pieces ended."""
    try: return json.loads(path.read_text())
    except (OSError, ValueError): return {'started': time.time(), 'fresh_done': False, 'done': 0}


PLATFORM_KEYS = ('machine.pvpanic', 'machine.smbios', 'machine.ps2')
PLATFORM_OFF = tuple(f'{k}=false' for k in PLATFORM_KEYS)
CLI_CONFIG = 'crates/cli/src/config.rs'


def platform_off(rev: str | None = None, root: pathlib.Path | None = None) -> list[str]:
    """`KEY=false` for each of PLATFORM_KEYS that the source tree a natemu build came from names in
    crates/cli/src/config.rs: the worktree's for this checkout's build (rev None; artifacts.natemu() builds the worktree
    as it is), the commit's for `rev` (artifacts.natemu_at: a TESTKIT_BENCH_AT round, a case's `baseline`). Per key, so
    a commit that has some of the devices (each on by default) runs with those off. A text check of the tree, never a
    run of the build: an older build has no such keys and refuses an unknown --set key, so it gets none (and runs as it
    always did)."""
    root = root or paths.repo_root()
    if rev is None:
        f = root / CLI_CONFIG
        text = f.read_text(errors='replace') if f.is_file() else ''
    else:
        # No config.rs at `rev` (an older tree) is no key; an unknown `rev` already fails in artifacts.natemu_at.
        r = subprocess.run(['git', '-C', str(root), 'show', f'{rev}:{CLI_CONFIG}'], capture_output=True, text=True)
        text = r.stdout if r.returncode == 0 else ''
    return [f'{k}=false' for k in PLATFORM_KEYS if k in text]


def _platform_args(spec, rev: str | None) -> list[str]:
    """The runner options that turn the platform devices off in the case's natemu CLI builds (module docstring)."""
    out = []
    if _uses(spec, 'natemu') or _uses(spec, 'natemu_entry'):
        out += [w for kv in platform_off(rev) for w in ('--natemu-set', kv)]
    if _uses(spec, 'natemu_baseline'):
        out += [w for kv in platform_off(spec['baseline']) for w in ('--baseline-set', kv)]
    return out


VCPUS_ENV = 'TESTKIT_BENCH_VCPUS'  # {vcpus} in a case's argv (the guest benchmarks): 1 unless set
SAMPLE_US_ENV = 'TESTKIT_SAMPLE_US'  # {sample_us} in a case's argv (the EL0 sampler's interval in us): SAMPLE_US unless set
SAMPLE_US = '250'
AT_ENV = 'TESTKIT_BENCH_AT'      # a revision: the run measures natemu and fexc as committed there (the same cases and inputs)


class Bench:
    def cases(self, fam, suite):
        """A case with `pieces = true` (a script that resumes from its own records, run_m1_perf.py full) becomes one case
        per run it plans, `<case>/piece-NN`, each running the next pending run (`--limit 1`): short exclusive units the
        scheduler can interleave with other work, instead of one indivisible window of an hour or more. The piece that
        finds nothing left reports the verdict."""
        out = []
        for name, spec in suite.extra.get('cases', {}).items():
            n = _piece_count(spec) if spec.get('pieces') else 0
            out += [f'{name}/piece-{i:02d}' for i in range(n)] if n else [name]
        return out

    def _spec(self, suite, case) -> tuple[str, dict, dict]:
        """(base case name, the base spec that keys its resume folder, the spec this case runs)."""
        base, _, piece = case.partition('/piece-')
        spec = suite.extra['cases'][base]
        if not piece: return base, spec, spec
        return base, spec, dict(spec, argv=[*spec['argv'], '--limit', '1'])

    def case_timeout(self, fam, suite, case):
        """The case's own `timeout` (seconds), else None: the suite's `params.timeout` applies (as the runner harness)."""
        return self._spec(suite, case)[2].get('timeout')

    def case_needs(self, fam, suite, case, variant=None):
        if self._spec(suite, case)[2].get('remote'):       # `remote = true`: the case's VM runs on another machine and times
            return Needs(cores=1)                          # nothing here (io5-solo32-reference); it only waits, on one core
        return Needs(exclusive=True, why='measures wall-clock time: nothing else may run')

    def requirements(self, fam, suite, variant):
        reqs = [Requirement('artifact', 'natemu'), Requirement('artifact', 'fexc'), Requirement('resource', 'debian-kernel'),
                runner.TARGET]
        for spec in suite.extra.get('cases', {}).values():
            if _uses(spec, 'bench_image'): reqs.append(Requirement('artifact', 'bench-image'))
            if _uses(spec, 'runtime_perf'): reqs.append(Requirement('artifact', 'perf-runtime'))
            if _uses(spec, 'runtime_profile'): reqs.append(Requirement('artifact', 'profile-runtime'))
            if _uses(spec, 'natemu_baseline'): reqs.append(Requirement('natemu-at', spec['baseline']))
            reqs += [Requirement('input', n) for p, n in INPUTS.items() if _uses(spec, p)]
            if _uses(spec, 'qemu_img') or _uses(spec, 'qemu_system'): reqs.append(Requirement('artifact', 'qemu-system'))
        return list(dict.fromkeys(reqs))

    def resolve(self, req):
        if req.kind == 'resource': return [resources.fetch(req.name)]
        if req == runner.TARGET: return runner.resolve_target(req)
        if req.kind == 'natemu-at': return [artifacts.natemu_at(req.name).entry]
        if req.kind == 'input': return [INPUT_FNS[req.name]()]
        return artifacts.resolve(req.name)

    def _subst(self, s, d):
        return s.format(**d)

    def _values(self, spec, d, n, fexc, raw, case='', machine='', fresh=False, platform=()) -> dict:
        vals = {'out': d, 'fexc': fexc, 'natemu': n.stub, 'runtime': n.runtime, 'host': n.host, 'vcpus': os.environ.get(VCPUS_ENV, '1'), 'sample_us': os.environ.get(SAMPLE_US_ENV, SAMPLE_US),
                'kernel': resources.fetch('debian-kernel'), 'raw': raw or '/nonexistent-natemu-raw',
                'bench_image': artifacts.bench_image() if _uses(spec, 'bench_image') else ''}
        if _uses(spec, 'natemu_entry'): vals['natemu_entry'] = n.entry
        if _uses(spec, 'runtime_perf'): vals['runtime_perf'] = artifacts.perf_runtime()
        if _uses(spec, 'runtime_profile'): vals['runtime_profile'] = artifacts.profile_runtime()
        if _uses(spec, 'natemu_baseline'): vals['natemu_baseline'] = artifacts.natemu_at(spec['baseline']).entry
        if _uses(spec, 'qemu_system'): vals['qemu_system'] = artifacts.qemu_system() / 'qemu-system-x86_64'
        if _uses(spec, 'qemu_img'): vals['qemu_img'] = artifacts.qemu_system() / 'qemu-img'
        ident = {k: str(v) for k, v in vals.items() if k not in ('out', 'raw') and _uses(spec, k)}
        if platform: ident['platform'] = ' '.join(platform)      # the switches are part of what a resumed record measured
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
        rev = os.environ.get(AT_ENV) or None
        if rev: n, fexc = artifacts.natemu_at(rev), artifacts.fexc_at(rev)     # a baseline round
        h = host.detect()
        out = {}
        for it in batch:
            base, keyspec, spec = self._spec(it.suite, it.case)
            d = runloop.run_dir(config) / 'bench' / h.machine_id / it.case
            d.mkdir(parents=True, exist_ok=True)
            raw = raw_root()
            piece = spec is not keyspec
            platform = _platform_args(keyspec, rev)       # after the case's own words, before a piece's --limit 1
            k = len(keyspec['argv'])
            spec = dict(spec, argv=[*spec['argv'][:k], *platform, *spec['argv'][k:]])
            if piece:            # one session file per case and run: pieces run in any order
                sf = d.parent / f'{base}.session.json'; sess = _session(sf)
                fresh = bool(config and config.getoption('fresh')) and not sess['fresh_done']     # once, whichever piece is first
                sess['fresh_done'] = sess['fresh_done'] or fresh
                spec = dict(spec, argv=[x for a in spec['argv'] for x in
                                        (['--retry-failed-before', str(sess['started'])] if a == '--retry-failed' else [a])])
                sf.write_text(json.dumps(sess))
            else:
                fresh = bool(config and config.getoption('fresh'))
            vals = self._values(keyspec, d, n, fexc, raw, base, h.machine_id, fresh, platform)
            where = f'{d}' + (f' (results in {vals["resume_dir"]}, kept for resuming)' if 'resume_dir' in vals else '')
            missing = [p for p in (self._subst(x, vals) for x in spec.get('needs', [])) if not pathlib.Path(p).exists()]
            if missing:
                out[it.nodeid] = CaseResult('skipped', f'needs {missing[0]} (not on this machine)', 'not-runnable'); continue
            if spec.get('remote') and not (os.environ.get('TESTKIT_SEED_ANSWERS') and os.environ.get('NATEMU_X86_HOST')):
                out[it.nodeid] = CaseResult('skipped', 'records a reference on the x86 machine: only with TESTKIT_SEED_ANSWERS=1 '
                                                       'NATEMU_X86_HOST=user@host', 'not-runnable'); continue
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
            if piece:
                sess = _session(sf); sess['done'] += 1; sf.write_text(json.dumps(sess))
            if r.returncode == 4 and piece:                            # its run was ok and runs are left
                total = len([c for c in self.cases(None, it.suite) if c.startswith(base + '/piece-')])
                if sess['done'] >= total:       # every piece of the case has run in this session and runs are still left
                    out[it.nodeid] = CaseResult('failed', f'runs left after all {total} pieces (a crash or retry shifted them): '
                                                          f'run the case again to finish them; {where}\n' + '\n'.join(tail))
                else:
                    out[it.nodeid] = CaseResult('passed', f'{took:.0f} s: one run measured, more left; {where}', 'measured')
                continue
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
