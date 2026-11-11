#!/usr/bin/env python3
"""Stored benchmark baselines with a drift canary: a few minutes of fixed measurements that say whether the machine still
behaves as it did when a baseline was measured, so a comparison need not re-measure the baseline every time.

    python3 tools/bench/canary.py record NAME --exe-dir DIR [--attach FILE ...]   # canary on the baseline build, stored
    python3 tools/bench/canary.py check  NAME [--tol 0.05]                        # exit 0 reuse, 3 re-measure

DIR holds the BASELINE build (`natemu`, `natemu-host`, `natemu-runtime`; a pic-style A/ directory). `record` runs the canary
with it and writes NATEMU_RAW_ROOT/bench-baselines/NAME.json (canary values, DIR, build commit, date and the --attach paths:
the baseline's own full results, which the comparison then reads instead of re-running the baseline). `check` runs the same
canary again on the same stored build: every value within TOL (relative) of the recorded one means the stored baseline is
still valid for a new A/B (reuse it, measure only the candidate); any value outside means the machine, load or thermal state
differs: re-measure the baseline, then `record` again. The canary (one VM at a time under the shared VM lock, about 3 min):
  boot      natemu stub, no initrd, 1 vCPU, to the "VFS: Unable to mount root fs" line (tools/bench/boot.py), best of 2
  cpython   the CPython slice (cpython-profile-20261110-01 initrd, part=slice), 2 vCPUs, seconds to its CT-END line
  micro     run_natemu.py benchmarks int and mem64, ns per guest instruction (median of 3)
A fio job is not in the canary (it needs the staged installed-root inputs); add one as another item of ITEMS when wanted.
Paired A/B runs (baseline and candidate side by side, so drift cancels) are not implemented. Fixed inputs: KERNEL, INITRD."""
import argparse, fcntl, json, os, pathlib, signal, subprocess, sys, tempfile, time

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
HERE = pathlib.Path(__file__).resolve().parent
RAW = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT', ROOT.parent / 'natemu-raw'))
KERNEL = RAW / 'kernel-census-20261028-01/dl/linux'
INITRD = RAW / 'cpython-profile-20261110-01/work/initrd.cpio.zst'
FEXC = RAW / 'correctness-forms-20261107-02/candidate-bin/fexc'


def boot(exe, work):
    walls = []
    for i in range(2):
        r = subprocess.run([sys.executable, HERE / 'boot.py', '--out', work, '--name', f'boot{i}', '--emu', 'natemu', '--vcpus', '1',
                            '--kernel', KERNEL, '--fexc', FEXC, '--natemu', exe / 'natemu', '--runtime', exe / 'natemu-runtime',
                            '--cache', 'off'], capture_output=True, text=True)
        rec = json.loads(r.stdout.strip().splitlines()[-1])
        assert rec['reached'] and not rec['contended'], f'boot {i}: {rec}'
        walls.append(rec['wall_s'])
    return {'boot_s': min(walls)}


def cpython(exe, work, cap=600):
    serial = work / 'cpython.serial'
    cmd = [exe / 'natemu', '--runtime', exe / 'natemu-runtime', '--fexc', FEXC, '--kernel', KERNEL, '--initrd', INITRD, '--vcpus', '2',
           '--ram', '3G', '--compile-cache', 'off', '--serial', serial, '--cmdline', 'console=ttyS0 mitigations=off nokaslr ct=prof part=slice']
    t0 = time.monotonic()
    p = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    end = None
    while end is None and time.monotonic() - t0 < cap and p.poll() is None:
        time.sleep(0.25)
        if serial.exists() and 'CT-END' in serial.read_text(errors='replace'): end = time.monotonic() - t0
    if p.poll() is None:
        p.send_signal(signal.SIGINT)
        try: p.wait(15)
        except subprocess.TimeoutExpired: p.kill(); p.wait()
    assert end is not None, 'cpython slice: no CT-END line'
    return {'cpython_s': round(end, 3)}


def micro(exe, work):
    tgt = work / 'tgt'      # the build as a cargo target directory, for the runners' NATEMU_PREBUILT
    (tgt / 'release').mkdir(parents=True); (tgt / 'aarch64-unknown-none/release').mkdir(parents=True)
    (tgt / 'release/natemu-host').symlink_to(exe / 'natemu-host')
    (tgt / 'aarch64-unknown-none/release/natemu-runtime').symlink_to(exe / 'natemu-runtime')
    out = work / 'micro'
    subprocess.run([sys.executable, HERE / 'run_natemu.py', '--fexc', FEXC, '--out', out, '--reps', '3', '--bench', 'int,mem64'],
                   check=True, capture_output=True, env=dict(os.environ, CARGO_TARGET_DIR=str(tgt), NATEMU_PREBUILT='1', NATEMU_RAW_ROOT=str(RAW)))
    return {f"micro_{r['name']}_ns_insn": round(r['ns_per_insn']['median'], 4) for r in json.loads((out / 'results.json').read_text())['results']}


ITEMS = (boot, cpython, micro)


def measure(exe):
    values = {}
    with tempfile.TemporaryDirectory() as d, open(RAW / '.vm.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for item in ITEMS:
            work = pathlib.Path(d) / item.__name__; work.mkdir()
            values.update(item(exe, work))
            print(f'  {item.__name__}: done', flush=True)
    return values


def stored(name): return RAW / 'bench-baselines' / f'{name}.json'


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('record'); r.add_argument('name'); r.add_argument('--exe-dir', type=pathlib.Path, required=True)
    r.add_argument('--attach', nargs='*', default=[], help='the baseline results this record vouches for (paths, stored as given)')
    c = sub.add_parser('check'); c.add_argument('name'); c.add_argument('--tol', type=float, default=0.05)
    a = ap.parse_args()
    if a.cmd == 'record':
        exe = a.exe_dir.resolve()
        commit = (exe / 'commit').read_text().strip() if (exe / 'commit').exists() else None
        rec = {'name': a.name, 'exe_dir': str(exe), 'commit': commit, 'date': time.strftime('%Y-%m-%d %H:%M:%S'), 'attached': a.attach,
               'canary': measure(exe)}
        stored(a.name).parent.mkdir(exist_ok=True); stored(a.name).write_text(json.dumps(rec, indent=1) + '\n')
        print(json.dumps(rec['canary'])); return
    rec = json.loads(stored(a.name).read_text())
    now, bad = measure(pathlib.Path(rec['exe_dir'])), []
    for k, v in rec['canary'].items():
        drift = now[k] / v - 1
        print(f'{k:28} stored {v:<10} now {now[k]:<10} {drift:+.1%}')
        if abs(drift) > a.tol: bad.append(k)
    print(f"{'REUSE the stored baseline' if not bad else 'RE-MEASURE the baseline (drift in ' + ', '.join(bad) + ')'} (tolerance {a.tol:.0%}); attached: {rec['attached']}")
    sys.exit(3 if bad else 0)


if __name__ == '__main__': main()
