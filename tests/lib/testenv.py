"""What every script under tests/ imports first: the import roots and the guard (tests/README.md).

Import roots: tests/lib, every family folder under tests/, tools and tools/reference, so a script finds the modules it
shares with other families wherever they live. A script starts with

    import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv

and calls `testenv.require_framework(__file__)` first thing when run as a program. That refuses to run outside the test
framework: a script a suite runs is started by `tools/natemu-test`, which queues its VMs, gives it the framework's inputs
and locked answers, and judges it; started by hand it would run against other inputs and live QEMU, and its result
would mean nothing. A script no suite runs (a diagnostic or a measurement) runs under `tools/vmlock.py`, which queues it
with the framework's work. Importing a script (unit tests do) is never refused.
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
TESTS = ROOT / 'tests'
ENV = 'NATEMU_TESTKIT'          # set by tools/natemu-test for everything it starts; 'manual:NAME' under tools/vmlock.py


def import_roots() -> list[pathlib.Path]:
    fams = sorted(d for d in TESTS.iterdir() if d.is_dir() and d.name != 'lib' and (d / 'family.toml').exists())
    return [TESTS / 'lib', *fams, ROOT / 'tools', ROOT / 'tools' / 'reference']


def _add_roots() -> None:
    have = set(sys.path)
    sys.path[1:1] = [str(p) for p in import_roots() if str(p) not in have]   # after the script's own folder


def suite_cases(script: pathlib.Path) -> list[tuple[str, str]]:
    """(family folder, case) for every runner case whose command runs `script`."""
    import tomllib
    rel = script.resolve().relative_to(ROOT).as_posix()
    out = []
    for f in sorted(TESTS.glob('*/family.toml')):
        for suite in tomllib.loads(f.read_text()).get('suite', []):
            for case, spec in suite.get('cases', {}).items():
                if any(rel in str(a) for a in spec.get('argv', [])): out.append((f.parent.name, case))
    return out


def require_framework(script: str) -> None:
    if os.environ.get(ENV): return
    path = pathlib.Path(script).resolve()
    rel = path.relative_to(ROOT).as_posix()
    cases = suite_cases(path)
    if not cases:
        if os.environ.get('NATEMU_VMLOCK_HELD'):          # tools/vmlock.py queued it; the runners it starts may run too
            os.environ[ENV] = f'manual:{path.stem}'
            return
        sys.exit(f'{rel} is not run by any suite: run it under the machine\'s queue,\n'
                 f'    python3 tools/vmlock.py shared python3 {rel} ...      (exclusive for a timed run)\n'
                 f'(tests/README.md).')
    fam = cases[0][0]
    names = ' or '.join(f'-k {c}' for _, c in cases[:4]) + (' ...' if len(cases) > 4 else '')
    sys.exit(f'{rel} is a test runner: run it through the test framework, which queues its VMs, supplies its inputs and\n'
             f'checks it against the locked answers:\n'
             f'    tools/natemu-test run tests/{fam} {names} --keep-outputs\n'
             f'The case\'s log is .natemu/runs/<run>/runner/<case>.log, with the exact command line it ran (docs/testing.md).')


_add_roots()
