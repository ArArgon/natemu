#!/usr/bin/env python3
"""The io family's campaign cases that run in two steps, prepare then run (tests/io/family.toml, suite io-campaign):
serial-unpaced and serial-paced (tests/io/run_serial_stress.py, factor 1), serial-loaded (factor 3, with the CPU load, unpaced)
and soak (tests/io/run_io_soak.py). Inputs come from $NATEMU_RAW_ROOT (the framework's input root): the matrix inputs and
the pinned stress-ng. Prepare goes to OUT/inputs, the run to OUT/run, and OUT/results.json gets
{"results": [{"name", "ok", "problems"}]} from the run's own result files. Exit status 0 when it passed, else 1."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import io_common  # noqa: E402

MATRIX = 'io-matrix-inputs-20261118-01'
STRESS = 'io-safety-stress-20261108-01'
CASES = ('serial-unpaced', 'serial-paced', 'serial-loaded', 'soak')


def plan(case, root, host, runtime, natemu, fexc, out):
    """(prepare argv, run argv, matrix folder to clone first, the clone's folder): the last two None for serial cases.
    Argument assembly only. `host` is unused: the runners start the natemu stub, `natemu`, next to it."""
    root, out = Path(root), Path(out)
    stress = root / STRESS
    load = ['--stress', stress / 'bin/stress-ng', '--stress-source', stress / 'source.tar.xz', '--stress-provenance', stress / 'provenance.json']
    py = [sys.executable, HERE / ('run_io_soak.py' if case == 'soak' else 'run_serial_stress.py')]
    if case == 'soak':
        clone = out / 'matrix-clone'
        prepare = py + ['prepare', '--matrix-inputs', clone, *load, '--out', out / 'inputs']
        run = py + ['run', '--backend', 'natemu', '--executable', natemu, '--inputs', out / 'inputs', '--out', out / 'run',
                    '--lock', io_common.VM_LOCK, '--runtime', runtime, '--fexc', fexc,
                    '--build-manifest', out / 'build-manifest.json']
        return [str(a) for a in prepare], [str(a) for a in run], root / MATRIX, clone
    mode = 'paced' if case == 'serial-paced' else 'unpaced'
    prepare = py + ['prepare', '--matrix-inputs', root / MATRIX, '--out', out / 'inputs']
    if case == 'serial-loaded': prepare += ['--factor', '3', '--cpu-load', *load]
    run = py + ['run', '--mode', mode, '--inputs', out / 'inputs', '--natemu', natemu, '--runtime', runtime, '--fexc', fexc, '--out', out / 'run']
    return [str(a) for a in prepare], [str(a) for a in run], None, None


def problems(case, out, status):
    """What the run's own result files say: serial-*: run/result.json {"pass", "errors"}; soak: run/results.json, one
    entry per phase (write, verify), each with "errors"."""
    out = Path(out)
    try:
        if case == 'soak':
            rs = json.loads((out / 'run/results.json').read_text())
            found = [f'{r.get("phase")}: {e}' for r in rs for e in r.get('errors', [])]
            if sorted(str(r.get('phase')) for r in rs) != ['verify', 'write']:
                found.append('phases run: ' + ', '.join(str(r.get('phase')) for r in rs))
        else:
            r = json.loads((out / 'run/result.json').read_text())
            found = list(r.get('errors') or []) + ([] if r.get('pass') else ['pass is false'])
    except (OSError, ValueError) as e:
        found = [f'no result file: {e}']
    if status and not found: found.append(f'exit status {status}')
    return found


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('case', choices=CASES)
    for n in ('host', 'runtime', 'natemu', 'fexc', 'out'): p.add_argument('--' + n, type=Path, required=True)
    a = p.parse_args(argv)
    a.out = a.out.resolve(); a.out.mkdir(parents=True, exist_ok=True)
    root = Path(os.environ['NATEMU_RAW_ROOT'])
    prepare, run, src, clone = plan(a.case, root, a.host, a.runtime, a.natemu, a.fexc, a.out)
    if clone is not None:
        io_common.writable_clone(src, clone)
        # run_io_soak binds the binaries it runs to a build manifest: {'artifacts': {path: sha256}}
        io_common.write_json(a.out / 'build-manifest.json', {'artifacts': {str(p.resolve()): io_common.digest(p.resolve()) for p in (a.natemu, a.runtime, a.fexc)}})
    status, step_failed = 0, None
    for name, step in (('prepare', prepare), ('run', run)):
        status = subprocess.run(step).returncode
        if status: step_failed = name; break
    found = problems(a.case, a.out, status) if step_failed != 'prepare' else [f'prepare exit status {status}']
    (a.out / 'results.json').write_text(json.dumps({'results': [{'name': a.case, 'ok': not found, 'problems': found}]}, indent=1) + '\n')
    return 1 if found else 0


if __name__ == '__main__':
    testenv.require_framework(__file__)
    raise SystemExit(main())
