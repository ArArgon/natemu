#!/usr/bin/env python3
"""Reference results from real x86 hardware: each program's ELF (ref.py's, the same harness QEMU runs) executed natively
on a Linux x86-64 machine over ssh, written in ref.py's results format so `run_slice1_vm.py` compares natemu with it.

    python3 tools/reference/ref_native.py PROGRAMS.json -o EXPECTED.json [--host USER@X86HOST] [--require-flag sha_ni]

For instructions pinned QEMU 7.2 does not implement (SHA: `gen_sha.py`). Only non-flow programs without fault mode. The
RFLAGS compare mask comes from the programs' annotations (flags.py), as in ref.py; the disassembly cross-check is QEMU's
log and is not made. The machine's `uname -a` and CPU model are recorded.
"""
import argparse, io, json, os, pathlib, struct, subprocess, sys, tarfile
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ref, flags

REMOTE = 'cd "$(mktemp -d)" && tar x && for f in p*.elf; do chmod +x $f; ./$f > ${f%.elf}.out; echo "$f $?" >> rc; done; ' \
         '{ uname -a; grep -m1 "model name" /proc/cpuinfo; grep -m1 -o -w "FLAG" /proc/cpuinfo; } > env; tar c *.out rc env; rm -rf "$PWD"'


def rosetta_elf(elf: bytes) -> bytes:
    """Rosetta's ELF loader requires the first PT_LOAD at file offset 0 ("first load segment not at file offset 0"), which
    Linux and QEMU do not: extend that segment backwards over the headers (one page below its address, unused by the
    harness), leaving every address the program sees unchanged."""
    e = bytearray(elf)
    phoff, = struct.unpack_from('<Q', e, 0x20)
    t, fl, off, va, pa, fs, ms, al = struct.unpack_from('<IIQQQQQQ', e, phoff)
    if off:
        ref.require(off % 0x1000 == 0 and va >= off, 'first segment cannot be extended to file offset 0')
        struct.pack_into('<IIQQQQQQ', e, phoff, t, fl, 0, va - off, pa - off, fs + off, ms + off, al)
    return bytes(e)


def execute(progs, host=None, rosetta=None, require_flag=None, timeout=600):
    """Run normalized programs (ref.norm) on the x86 machine over ssh, or here through Rosetta with `rosetta` (an image):
    [(exit status, output bytes)] per program, and the machine's description (uname, CPU model, the flag if reported).
    Any program kind: flow and fault-mode programs run too (the caller derives their compare mask; ref_native's own
    command line takes plain programs only, since their mask comes from the annotations)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as t:
        for i, p in enumerate(progs):
            elf = ref.build_elf(p)[0]
            if rosetta: elf = rosetta_elf(elf)
            info = tarfile.TarInfo(f'p{i:04d}.elf'); info.size = len(elf); info.mode = 0o755
            t.addfile(info, io.BytesIO(elf))
    script = REMOTE.replace('FLAG', require_flag or 'fpu')
    cmd = (['container', 'run', '--rm', '-i', '--arch', 'amd64', '--network', 'none', rosetta, 'sh', '-c', script] if rosetta
           else ['ssh', '-o', 'BatchMode=yes', host, script])
    r = subprocess.run(cmd, input=buf.getvalue(), capture_output=True, timeout=timeout)
    ref.require(r.returncode == 0, cmd[0] + ': ' + r.stderr.decode()[-500:])
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as t:
        files = {m.name: t.extractfile(m).read() for m in t.getmembers()}
    rcs = dict(line.split() for line in files['rc'].decode().splitlines())
    return [(rcs[f'p{i:04d}.elf'], files.get(f'p{i:04d}.out', b'')) for i in range(len(progs))], \
        files['env'].decode().strip().splitlines()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('programs', type=pathlib.Path); ap.add_argument('-o', '--output', type=pathlib.Path, required=True)
    ap.add_argument('--host', default=os.environ.get('NATEMU_X86_HOST'),
                    help='the x86-64 Linux machine (user@host) to run on over ssh (default: NATEMU_X86_HOST)')
    ap.add_argument('--require-flag', default='sha_ni', help='a /proc/cpuinfo flag the machine must report (default sha_ni; '
                    "'' for none)")
    ap.add_argument('--rosetta', metavar='IMAGE', help='run the ELFs here, in an amd64 Linux container through Rosetta '
                    '(Apple `container`, pinned IMAGE by digest), instead of over ssh')
    a = ap.parse_args()
    if not a.host and not a.rosetta: ap.error('needs an x86-64 Linux machine: --host USER@HOST or NATEMU_X86_HOST (or --rosetta IMAGE)')
    programs = json.loads(a.programs.read_text())['programs']
    progs = [ref.norm(p) for p in programs]
    ref.require(not any(p['flow'] or p['faults'] is not None or p['prefix'] is not None for p in progs), 'plain programs only')
    outs, env = execute(progs, a.host, a.rosetta, a.require_flag)
    results = []
    for i, (src, p) in enumerate(zip(programs, progs)):
        rc, out = outs[i]
        ref.require(rc == '0', f"exit status {rc} for {p['name']} (a signal: the instruction faulted)")
        final, _ = ref.parse_output(p, out)
        und = flags.analyze(p['insns'])
        results.append({'name': p['name'], 'qemu': final, 'undefined_flags': ref.hx(und), 'rflags_compare_mask': ref.hx(flags.TRACKED & ~und)})
    ref.require(not a.require_flag or a.require_flag in env, f'the machine does not report {a.require_flag}')
    a.output.write_text(json.dumps({'format': ref.FORMAT, 'reference': 'rosetta' if a.rosetta else 'native',
                                    'host': a.rosetta or a.host, 'machine': env,
                                    'rflags_fixed_bits': ref.hx(flags.FIXED), 'results': results}, indent=1) + '\n')
    print(f"{len(results)} programs run {'through Rosetta' if a.rosetta else 'natively'} on {env[1] if len(env) > 1 else a.host}; "
          f'wrote {a.output}')

if __name__ == '__main__': main()
