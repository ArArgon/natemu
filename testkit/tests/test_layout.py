"""The test scripts' layout (tests/README.md): every script under tests/ finds its imports through tests/lib/testenv.py,
refuses to run outside the framework, and each family's `covers` names every tests/ folder its runners reach."""
import ast
import os
import pathlib
import re
import subprocess
import sys
import tomllib

from testkit import paths

ROOT = paths.repo_root()
SCRIPTS = sorted(p for p in (ROOT / 'tests').glob('*/*.py') if p.name != 'testenv.py')
BOOT = "import testenv"


def _mains(p):
    return re.search(r"^if __name__ ?== ?['\"]__main__['\"]:", p.read_text(), re.M)


def test_no_test_script_is_left_in_tools():
    left = [p.name for p in (ROOT / 'tools').glob('run_*.py')]
    assert not left, f'test runners belong in tests/<family>/ (tests/README.md): {left}'


def test_every_script_bootstraps_and_every_program_is_guarded():
    loaded_by_testkit = ('rules.py', 'judges.py', 'verdicts.py')       # loaded by path from testkit, not run
    no_boot = [p.relative_to(ROOT).as_posix() for p in SCRIPTS if p.name not in loaded_by_testkit and BOOT not in p.read_text()]
    assert not no_boot, no_boot
    unguarded = [p.relative_to(ROOT).as_posix() for p in SCRIPTS
                 if not p.name.startswith('test_') and _mains(p) and 'testenv.require_framework(__file__)' not in p.read_text()]
    assert not unguarded, unguarded


def test_a_runner_started_by_hand_says_how_to_run_it():
    env = {k: v for k, v in os.environ.items() if k not in ('NATEMU_TESTKIT', 'NATEMU_VMLOCK_HELD')}
    r = subprocess.run([sys.executable, 'tests/faults/run_i5c.py', '--help'], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode != 0 and 'tools/natemu-test run tests/faults -k i5c' in r.stderr, r.stderr
    r = subprocess.run([sys.executable, 'tests/system/run_relink_mutations.py', '--list'], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode != 0 and 'tools/vmlock.py' in r.stderr, r.stderr


# Scripts a family's harness or rules import into the testkit process itself, not started through argv.
IN_PROCESS = {'programs': ['tests/lib/run_slice1_vm.py', 'tests/lib/testenv.py'],
              'fp': ['tests/lib/run_slice1_vm.py', 'tests/lib/fp_deviations.py', 'tests/lib/testenv.py']}


def _closure(start, roots):
    def find(mod, base):
        for d in (base, *roots):
            p = d / (mod.split('.')[0] + '.py')
            if p.is_file(): return p.resolve()
    seen, todo = set(), [start.resolve()]
    while todo:
        f = todo.pop()
        if f in seen: continue
        seen.add(f)
        text = f.read_text()
        for n in ast.walk(ast.parse(text)):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) else \
                [n.module] if isinstance(n, ast.ImportFrom) and n.module and not n.level else []
            todo += [p for m in mods if (p := find(m, f.parent))]
        todo += [p for m in re.findall(r"['\"/]([a-z0-9_]+)\.py['\"]", text) if (p := find(m, f.parent))]   # started by path
    return seen


def test_covers_name_every_tests_folder_a_family_reaches():
    from testkit import select
    sys.path.insert(0, str(ROOT / 'tests' / 'lib'))
    import testenv
    roots = testenv.import_roots()
    for fam in sorted((ROOT / 'tests').glob('*/family.toml')):
        d = tomllib.loads(fam.read_text())
        covers = d['family'].get('covers', [])
        reached = set()
        for script in IN_PROCESS.get(fam.parent.name, []): reached |= _closure(ROOT / script, roots)
        for suite in d.get('suite', []):
            for spec in suite.get('cases', {}).values():
                for a in spec.get('argv', []):
                    if re.fullmatch(r'tests/\w+/\w+\.py', a): reached |= _closure(ROOT / a, roots)
        missing = sorted({p.relative_to(ROOT).as_posix() for p in reached if p.is_relative_to(ROOT / 'tests')
                          and not select.matches(p.relative_to(ROOT).as_posix(), covers)})
        assert not missing, f'{fam.parent.name}: covers miss {missing}'
