#!/usr/bin/env python3
"""Build fixtures/slice1/{programs,expected}.json on demand from fixtures/slice1/manifest.json.

    tools/with-env python3 tools/reference/build_slice1.py [--programs-only]

manifest.json (committed) holds the inputs: generator script + seed, and the pinned QEMU/image identity.  Gate: the
pins in ref.py (image id, QEMU sha256) and the header of expected.json must equal the manifest's, else the build fails.
Outputs are NOT hash-gated (host clang is not pinned; correctness comes from QEMU and ref.py's per-run checks).
Each build writes fixtures/slice1/build.json (git-ignored): inputs used, clang version, sha256 of both outputs, so a
verification run can cite its corpus.  A file is regenerated if it, or build.json, is missing or if its inputs changed
(seed, generator/reference source, pins).  programs.json needs host clang; expected.json also needs Docker and the
pinned image/QEMU (without them: exit nonzero with a message; programs.json is still built).
"""
import argparse, hashlib, json, os, pathlib, shutil, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
FIX = REPO / 'fixtures' / 'slice1'
PROGRAMS, EXPECTED, MANIFEST, RECORD = (FIX / n for n in ('programs.json', 'expected.json', 'manifest.json', 'build.json'))
sys.path.insert(0, str(HERE))
import ref


class Mismatch(Exception): """A pin differs from the manifest (a real defect: fails tests)."""
class Unavailable(Exception): """A prerequisite (Docker, pinned QEMU, clang) is missing (tests skip)."""


def sha(*paths): return hashlib.sha256(b''.join(p.read_bytes() for p in paths)).hexdigest()
def clang_version():
    if not shutil.which('clang'): raise Unavailable('clang not found (needed to generate programs.json)')
    return subprocess.run(['clang', '--version'], capture_output=True, text=True).stdout.splitlines()[0]
def raw_dir(): return pathlib.Path(os.environ.get('NATEMU_RAW_ROOT', REPO.parent / 'natemu-raw')) / 'reference-slice1-20261026-01'
def need_docker():
    if not shutil.which('docker') or subprocess.run(['docker', 'info'], capture_output=True).returncode:
        raise Unavailable('Docker is not available; expected.json cannot be built (programs.json is unaffected)')
    if not (raw_dir() / 'pin' / 'qemu-x86_64-static').exists():
        raise Unavailable(f'pinned QEMU missing at {raw_dir() / "pin"} (set NATEMU_RAW_ROOT)')


def check_pins(m):
    for k, got in (('image', ref.IMAGE), ('qemu_sha256', ref.PINS['qemu-x86_64-static'])):
        if m['qemu'][k] != got: raise Mismatch(f'ref.py pin {k} = {got} but manifest says {m["qemu"][k]}')
    if EXPECTED.exists():
        head = {k: v for k, v in json.loads(EXPECTED.read_text()).items() if k != 'results'}
        bad = {k: (head.get(k), v) for k, v in m['qemu'].items() if head.get(k) != v}
        if bad: raise Mismatch(f'expected.json header differs from manifest pins (file, manifest): {bad}')


def run(script, *args):
    subprocess.run([sys.executable, '-B', str(HERE / script), *map(str, args)], check=True, cwd=REPO, stdout=subprocess.DEVNULL)


def build(programs_only=False):
    """Ensure the files exist and are current for the manifest inputs; returns the list of files (re)generated."""
    m = json.loads(MANIFEST.read_text()); gen = m['generator']; check_pins(m)
    assert gen['script'] == 'tools/reference/gen_slice1.py', 'manifest names a different generator'
    rec = json.loads(RECORD.read_text()) if RECORD.exists() else {}
    p_in = {'seed': gen['seed'], 'generator_sha256': sha(REPO / gen['script'], HERE / 'flags.py', HERE / 'ref.py')}
    done = []
    if not (PROGRAMS.exists() and rec.get('programs_inputs') == p_in):
        rec['clang'] = clang_version(); new = PROGRAMS.with_name('programs.json.new')
        run('gen_slice1.py', '-o', new, '--seed', gen['seed']); os.replace(new, PROGRAMS); done.append('programs.json')
        rec['programs_inputs'] = p_in; rec.pop('expected_inputs', None)
    if not programs_only:
        e_in = {'programs_sha256': sha(PROGRAMS), 'qemu': m['qemu'], 'ref_sha256': sha(HERE / 'ref.py', HERE / 'flags.py', HERE / 'container_entry.py')}
        if not (EXPECTED.exists() and rec.get('expected_inputs') == e_in):
            need_docker(); raw = raw_dir(); (raw / 'run').mkdir(parents=True, exist_ok=True); new = EXPECTED.with_name('expected.json.new')
            run('ref.py', PROGRAMS, '-o', new, '--qemu', raw / 'pin' / 'qemu-x86_64-static', '--workdir', raw / 'run')
            os.replace(new, EXPECTED); done.append('expected.json'); rec['expected_inputs'] = e_in
        check_pins(m)
    rec['sha256'] = {'programs.json': sha(PROGRAMS), **({'expected.json': sha(EXPECTED)} if EXPECTED.exists() else {})}
    if done or not RECORD.exists(): RECORD.write_text(json.dumps(rec, indent=1) + '\n')
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--programs-only', action='store_true'); a = ap.parse_args()
    try: done = build(a.programs_only)
    except Mismatch as e: sys.exit(f'PIN MISMATCH: {e}')
    except Unavailable as e:
        try: build(programs_only=True); print('programs.json OK', file=sys.stderr)
        except (Mismatch, Unavailable) as e2: print(f'programs.json: {e2}', file=sys.stderr)
        sys.exit(f'UNAVAILABLE: {e}')
    print(('regenerated: ' + ', '.join(done)) if done else 'up to date', '| record:', RECORD.relative_to(REPO))

if __name__ == '__main__': main()
