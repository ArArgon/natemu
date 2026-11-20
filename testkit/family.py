"""Test families (spec R3): `tests/<family>/family.toml` declares inputs, suites, variants, claims and the paths it
covers; a family's `rules.py` decides which cases apply to a variant (R2.13); the family's `tests.lock` holds hashes.

    [family]
    name = "fp"
    covers = ["crates/runtime/src/**/fp*"]
    [input.i8]
    generator = "tools/reference/gen_i8.py"
    args = ["--seed", "20261030", "--random", "60"]
    sources = ["tools/reference/gen_slice1.py"]        # repo files the generator imports (fingerprinted)
    [[suite]]
    name = "i8"
    kind = "deterministic"                             # deterministic | judged | bench | units
    harness = "programs"                               # a testkit.harness module, or "file.py:Class" in this folder
    input = "i8"
    reference = "qemu"                                 # qemu | x86 (seeding only)
    matrix = { "engine.fp" = ["fast", "accurate"], "host.afp" = [true, false] }
    needs = { cores = 1 }                              # cores, exclusive + why, perf, dedicated, network
    smoke = ["i8-forms-000"]                           # or true: every case
    batch = 64
"""
import dataclasses
import functools
import importlib
import importlib.util
import itertools
import pathlib
import tomllib
from typing import Callable

from testkit import paths


class FamilyError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Needs:
    cores: int = 1
    exclusive: bool = False
    why: str = ''
    perf: bool = False
    dedicated: bool = False
    network: bool = False
    # with `perf` and more cores than the Mac has performance cores: take the whole machine instead (vCPU threads then run on
    # efficiency cores too). Never on an M1, whose efficiency cores have no TSO: there the claim stays unsatisfiable.
    mixed: bool = False


@dataclasses.dataclass
class InputDecl:
    name: str
    generator: str | None = None
    args: list = dataclasses.field(default_factory=list)
    sources: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Suite:
    name: str
    kind: str
    harness: str
    input: str | None = None
    reference: str | None = None
    matrix: dict = dataclasses.field(default_factory=dict)
    needs: Needs = Needs()
    smoke: object = False
    batch: int = 1
    judge: str | None = None
    params: dict = dataclasses.field(default_factory=dict)
    rerun: int = 0
    extra: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Family:
    name: str
    dir: pathlib.Path
    covers: list
    rules: Callable | None
    suites: list
    inputs: dict
    raw: dict

    def suite(self, name: str) -> Suite:
        return next(s for s in self.suites if s.name == name)


@dataclasses.dataclass(frozen=True)
class Variant:
    params: tuple           # ((key, value), ...) sorted by key

    @property
    def id(self) -> str:
        return ','.join(sorted(f'{k.rsplit(".", 1)[-1]}={v}' for k, v in self.params))

    def get(self, key, default=None):
        return dict(self.params).get(key, default)


SUITE_KEYS = {f.name for f in dataclasses.fields(Suite)} - {'extra', 'needs'}
KINDS = {'deterministic', 'judged', 'bench', 'units'}


def _load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


@functools.cache
def load(path: pathlib.Path) -> Family:
    path = pathlib.Path(path)
    d = tomllib.loads(path.read_text())
    fam = d.get('family', {})
    name = fam.get('name') or path.parent.name
    suites = []
    for s in d.get('suite', []):
        needs = dict(s.get('needs', {}))
        if needs.get('exclusive') and not needs.get('why'):
            raise FamilyError(f'{path}: suite {s.get("name")}: exclusive needs a why')
        if needs.get('dedicated'): needs['perf'] = True
        if s.get('kind') not in KINDS: raise FamilyError(f'{path}: suite {s.get("name")}: kind must be one of {sorted(KINDS)}')
        known = {k: v for k, v in s.items() if k in SUITE_KEYS}
        suites.append(Suite(**known, needs=Needs(**needs), extra={k: v for k, v in s.items() if k not in SUITE_KEYS | {'needs'}}))
    rules = None
    rf = path.parent / fam.get('rules', 'rules.py')
    if rf.exists(): rules = getattr(_load_module(rf, f'testkit_rules_{name}'), 'applies', None)
    inputs = {k: InputDecl(name=k, **v) for k, v in d.get('input', {}).items()}
    return Family(name, path.parent, fam.get('covers', []), rules, suites, inputs, d)


def families(root: pathlib.Path | None = None) -> list[Family]:
    root = root or paths.repo_root() / 'tests'
    return [load(p) for p in sorted(root.glob('*/family.toml'))]


def expand(suite: Suite) -> list[Variant]:
    keys = sorted(suite.matrix)
    vs = [Variant(tuple(zip(keys, combo))) for combo in itertools.product(*(suite.matrix[k] for k in keys))]
    return sorted(vs, key=lambda v: v.id) or [Variant(())]


@functools.cache
def features() -> dict:
    f = paths.repo_root() / 'tests' / 'features.toml'
    return tomllib.loads(f.read_text()).get('feature', {}) if f.exists() else {}


@dataclasses.dataclass(frozen=True)
class Placement:
    runnable: bool
    env: tuple = ()             # ((name, value), ...) to hide features
    met: tuple = ()             # ((feature, 'real' | 'hidden'), ...)
    reason: str = ''


def placement(variant: Variant, host, require_real: frozenset = frozenset()) -> Placement:
    """Can this machine run the variant (R5.3, R5.4a)? A machine that has a feature runs the variants that need it
    absent by hiding it, if the feature can be hidden and the requester did not ask for real absence."""
    env, met = {}, {}
    for key, need in variant.params:
        if not key.startswith('host.'): continue
        feat = key[5:]
        decl = features().get(feat, {})
        have = host.features.get(decl.get('detect', feat), False)
        if need and not have:
            return Placement(False, reason=f'needs {feat}')
        if need or not have:
            met[feat] = 'real'; continue
        if feat in require_real or not decl.get('hide_env'):
            return Placement(False, reason=f'needs {feat} absent (real)')
        env.update(decl['hide_env']); met[feat] = 'hidden'
    return Placement(True, tuple(sorted(env.items())), tuple(sorted(met.items())))


def harness_for(fam: Family, suite: Suite):
    """The suite's harness object: `testkit.harness.<name>`'s HARNESS, or `file.py:Name` in the family folder."""
    if ':' in suite.harness:
        f, cls = suite.harness.split(':')
        return getattr(_load_module(fam.dir / f, f'testkit_h_{fam.name}_{f[:-3]}'), cls)()
    return importlib.import_module(f'testkit.harness.{suite.harness}').HARNESS
