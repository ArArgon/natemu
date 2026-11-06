#!/usr/bin/env python3
"""Reference results from real x86 hardware: each program's ELF (ref.py's, the same harness QEMU runs) executed natively
on a Linux x86-64 machine over ssh, written in ref.py's results format so `run_slice1_vm.py` compares natemu with it.

    python3 tools/reference/ref_native.py PROGRAMS.json -o EXPECTED.json [--host ubuntu@192.168.12.11] [--require-flag sha_ni]

For instructions pinned QEMU 7.2 does not implement (SHA: `gen_sha.py`). Only non-flow programs without fault mode. The
RFLAGS compare mask comes from the programs' annotations (flags.py), as in ref.py; the disassembly cross-check is QEMU's
log and is not made. The machine's `uname -a` and CPU model are recorded.
"""
import argparse, io, json, pathlib, subprocess, sys, tarfile
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ref, flags

REMOTE = 'cd "$(mktemp -d)" && tar x && for f in p*.elf; do chmod +x $f; ./$f > ${f%.elf}.out; echo "$f $?" >> rc; done; ' \
         '{ uname -a; grep -m1 "model name" /proc/cpuinfo; grep -m1 -o -w "FLAG" /proc/cpuinfo; } > env; tar c *.out rc env; rm -rf "$PWD"'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('programs', type=pathlib.Path); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--host', default='ubuntu@192.168.12.11')
    ap.add_argument('--require-flag', default='sha_ni', help='a /proc/cpuinfo flag the machine must report (default sha_ni)')
    a = ap.parse_args()
    programs = json.loads(a.programs.read_text())['programs']
    progs = [ref.norm(p) for p in programs]
    ref.require(not any(p['flow'] or p['faults'] is not None or p['prefix'] is not None for p in progs), 'plain programs only')
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as t:
        for i, p in enumerate(progs):
            elf = ref.build_elf(p)[0]; info = tarfile.TarInfo(f'p{i:04d}.elf'); info.size = len(elf); info.mode = 0o755
            t.addfile(info, io.BytesIO(elf))
    r = subprocess.run(['ssh', '-o', 'BatchMode=yes', a.host, REMOTE.replace("FLAG", a.require_flag)], input=buf.getvalue(), capture_output=True, timeout=600)
    ref.require(r.returncode == 0, 'ssh: ' + r.stderr.decode()[-500:])
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as t:
        files = {m.name: t.extractfile(m).read() for m in t.getmembers()}
    rcs = dict(line.split() for line in files['rc'].decode().splitlines())
    results = []
    for i, (src, p) in enumerate(zip(programs, progs)):
        ref.require(rcs[f'p{i:04d}.elf'] == '0', f"exit status {rcs[f'p{i:04d}.elf']} for {p['name']} (a signal: the instruction faulted)")
        final, _ = ref.parse_output(p, files[f'p{i:04d}.out'])
        und = flags.analyze(p['insns'])
        results.append({'name': p['name'], 'qemu': final, 'undefined_flags': ref.hx(und), 'rflags_compare_mask': ref.hx(flags.TRACKED & ~und)})
    env = files['env'].decode().strip().splitlines()
    ref.require(a.require_flag in env, f'the machine does not report {a.require_flag}')
    a.output.write_text(json.dumps({'format': ref.FORMAT, 'reference': 'native', 'host': a.host, 'machine': env,
                                    'rflags_fixed_bits': ref.hx(flags.FIXED), 'results': results}, indent=1) + '\n')
    print(f'{len(results)} programs run natively on {env[1] if len(env) > 1 else a.host}; wrote {a.output}')

if __name__ == '__main__': main()
