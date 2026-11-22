"""The boot family's runner harness for suites whose cases read an input built in the amd64 builder container: the
real-code suites of the boot-time interpreter (kvm-unit-tests and OVMF; docs/restructure/real-code-suites.md).

    [[suite]] harness = "built_inputs.py:Runner"  inputs = ["kvm-unit-tests"]     (names of BUILDERS below)

Exactly testkit's runner harness (testkit/harness/runner.py) plus, per name in the suite's `inputs`, a requirement
prepared before any case starts (built once per machine from the recipe and the pinned downloads, then taken from the
cache; on the x86 builder when the machine has one, as testkit.inputs builds stress-ng) and, at run time, its folder in
the case's environment as NATEMU_INPUT_<NAME> (upper case, '-' as '_'). Each folder holds the built files and
provenance.json: `upstream`, what it is built from (each download's name and pinned sha256, the recipe folder's sha256,
the builder image), which keys the cases' answers, and `built` and `builder`, what came out and where (recorded, never
required: docs/testing.md, "Where amd64 inputs are built").

Loaded by testkit into its own process (testkit.family.harness_for), never run."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import hashlib
import inspect
import json
import os
import shutil

from testkit import artifacts, cache, inputs, paths, resources
from testkit.harness import Requirement, runner

KIND = 'built-input'


def _sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def build(name: str, recipe: pathlib.Path, sources: dict, outputs: list, out: pathlib.Path, timeout: int, memory: str):
    """recipe/container.sh in the builder container (testkit.inputs._in_container), with each source (download name ->
    file name) in /work; `outputs` and build.log moved to `out`, and provenance.json written there."""
    work = out / 'work'; work.mkdir()
    for res, fname in sources.items(): cache.clone(resources.fetch(res), work / fname)
    inputs._in_container(name, recipe, work, timeout=timeout, memory=memory)
    for f in [*outputs, 'build.log']:
        if not (work / f).is_file(): raise inputs.BuildFailure(name, f'the recipe wrote no {f}')
        os.replace(work / f, out / f)
    (out / 'provenance.json').write_text(json.dumps(provenance(name, recipe, sources, {f: _sha(out / f) for f in outputs}),
                                                    indent=1, sort_keys=True) + '\n')
    shutil.rmtree(work)


def provenance(name: str, recipe: pathlib.Path, sources: dict, built: dict) -> dict:
    return {'name': name,
            'upstream': {'sources': {res: resources.declarations()[res]['sha256'] for res in sorted(sources)},
                         'recipe_sha256': inputs.recipe_sha256(recipe), 'image': artifacts.BENCH_BUILDER},
            'built': built, 'builder': artifacts.build_env_described()}


KUT_SOURCES = {'kvm-unit-tests-src': 'src.tar.gz'}
KUT_OUTPUTS = ['dummy.flat', 'smptest.flat']


def _kut_recipe(out: pathlib.Path, ins: list) -> None:
    build('kvm-unit-tests', ins[0], KUT_SOURCES, KUT_OUTPUTS, out, timeout=1800, memory='4g')


def kvm_unit_tests() -> pathlib.Path:
    """kvm-unit-tests a2891d95's x86/dummy.flat and x86/smptest.flat (tools/boot/kvm-unit-tests), with provenance.json."""
    recipe = paths.repo_root() / 'tools' / 'boot' / 'kvm-unit-tests'
    srcs = [str(resources.fetch(r)) for r in sorted(KUT_SOURCES)]     # paths: keyed by their pins
    return resources.derived('boot-kvm-unit-tests', _kut_recipe,
                             [recipe, artifacts.build_env(), inspect.getsource(build) + inspect.getsource(provenance), *srcs])


OVMF_SOURCES = {'edk2-src': 'edk2.tar.gz', 'edk2-openssl-src': 'openssl.tar.gz', 'edk2-brotli-src': 'brotli.tar.gz',
                'edk2-mipisyst-src': 'mipisyst.tar.gz', 'edk2-mbedtls-src': 'mbedtls.tar.gz', 'edk2-libspdm-src': 'libspdm.tar.gz'}
OVMF_OUTPUTS = ['OVMF.fd', 'OVMF_CODE.fd', 'OVMF_VARS.fd', 'SECFV.Fv.map', 'ResetVector.bin', 'report.txt']


def _ovmf_recipe(out: pathlib.Path, ins: list) -> None:
    build('ovmf', ins[0], OVMF_SOURCES, OVMF_OUTPUTS, out, timeout=5400, memory='8g')


def ovmf() -> pathlib.Path:
    """edk2-stable202608's OvmfPkgX64 (DEBUG, GCC, no SMM, no Secure Boot; tools/boot/ovmf), with provenance.json."""
    recipe = paths.repo_root() / 'tools' / 'boot' / 'ovmf'
    srcs = [str(resources.fetch(r)) for r in sorted(OVMF_SOURCES)]
    return resources.derived('boot-ovmf', _ovmf_recipe,
                             [recipe, artifacts.build_env(), inspect.getsource(build) + inspect.getsource(provenance), *srcs])


BUILDERS = {'kvm-unit-tests': kvm_unit_tests, 'ovmf': ovmf}


def env_name(name: str) -> str:
    return 'NATEMU_INPUT_' + name.upper().replace('-', '_')


class Runner(runner.Runner):
    def _names(self, suite) -> list:
        names = list(suite.extra.get('inputs', []))
        if bad := [n for n in names if n not in BUILDERS]:
            raise ValueError(f'suite {suite.name}: no builder for inputs {bad} (tests/boot/built_inputs.py BUILDERS)')
        return names

    def requirements(self, fam, suite, variant):
        return super().requirements(fam, suite, variant) + [Requirement(KIND, n) for n in self._names(suite)]

    def resolve(self, req):
        if req.kind == KIND: return [BUILDERS[req.name]()]
        return super().resolve(req)

    def run(self, batch, config):
        for suite in {id(it.suite): it.suite for it in batch}.values():
            for n in self._names(suite): os.environ[env_name(n)] = str(BUILDERS[n]())   # the entry this run prepared
        return super().run(batch, config)
