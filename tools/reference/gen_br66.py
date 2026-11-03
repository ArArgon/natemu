#!/usr/bin/env python3
"""Seeded corpus: 66 (operand-size prefix) on near direct branches in 64-bit mode, with SDM-derived expected states.

    tools/with-env python3 tools/reference/gen_br66.py --out DIR [--seed N] [--qemu QEMU]

Intel ignores 66 on a near direct branch in 64-bit mode: the operand size is forced to 64 bits and the displacement stays
rel8/rel32 (SDM Vol.2 CALL: "the operand size for near call (and all near branches) is forced to 64-bits", "In 64-bit
mode the relative offset is always a 32-bit immediate value"; JMP/Jcc: the rel16 forms are "Not supported in 64-bit mode";
Appendix A, superscript f64). Programs are gen_rex.branches with a 66 before every branch (Jcc rel8/rel32 backward loops,
Jcc rel8, JMP rel8/rel32, CALL rel32 whose pushed address is popped, LOOP, JRCXZ), at a random place among 0-1 random
segment prefixes. Pinned QEMU 7.2 follows AMD for 66 with rel32 (it decodes rel16 and the run faults) and so is not the
reference for these bytes: the expected state is pinned QEMU's run of the twin program with each such 66 replaced by 3E
(a DS prefix, which the branch also ignores; same length, so same addresses and return addresses). QEMU runs without -d
(no annotation cross-check; the final RIP must be the capture sequence's). Writes DIR/programs.json, DIR/expected.json
(ref.py's result format) for tools/run_slice1_vm.py.
"""
import argparse, json, os, pathlib, random, struct, subprocess, sys, uuid
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import flags, gen_rex, ref


def pick(r):
    pre = r.sample(gen_rex.SEGS, r.randrange(0, 2))
    pre.insert(r.randrange(len(pre) + 1), 0x66)
    return pre


def pair(name, seed):
    """The program and its twin (3E for the 66), drawn from the same random sequence."""
    prog = gen_rex.branches(name, random.Random(seed), pick)
    twin = gen_rex.branches(name, random.Random(seed), lambda r: [0x3E if x == 0x66 else x for x in pick(r)])
    a, b = bytes.fromhex(prog['code']), bytes.fromhex(twin['code'])
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    assert len(a) == len(b) and len(diff) == 10 and all(a[i] == 0x66 and b[i] == 0x3E for i in diff), name
    return prog, twin


def run_qemu(twins, work, qemu):
    """Pinned QEMU (no -d) on each twin in the pinned image; returns ref.py-format results."""
    require = ref.require
    require(__import__('hashlib').sha256(qemu.read_bytes()).hexdigest() == ref.PINS['qemu-x86_64-static'], 'QEMU pin')
    progs = [ref.norm(t) for t in twins]
    for i, p in enumerate(progs):
        d = work / 'jobs' / f'p{i:04d}'; d.mkdir(parents=True)
        (d / 'prog.elf').write_bytes(ref.build_elf(p)[0]); (d / 'prog.elf').chmod(0o755); (d / 'cpu').write_text(p['cpu'])
    (work / 'entry.py').write_text(
        "import pathlib, subprocess\n"
        "for d in sorted(pathlib.Path('/work/jobs').iterdir()):\n"
        "    with open(d / 'qemu.stdout', 'wb') as o:\n"
        "        r = subprocess.run(['/qemu', '-cpu', (d / 'cpu').read_text(), 'prog.elf'], cwd=d, stdout=o, stderr=open(d / 'qemu.stderr', 'wb'), timeout=60)\n"
        "    (d / 'rc').write_text(str(r.returncode))\n")
    name = 'natemu-br66-' + uuid.uuid4().hex
    subprocess.run(['docker', 'create', '--name', name, '--network', 'none', '--read-only', '--cpus', '2', '--memory', '512m',
                    '--pids-limit', '128', '--tmpfs', '/tmp:rw,size=32m', '--mount', f'type=bind,source={work},target=/work',
                    '--mount', f'type=bind,source={qemu},target=/qemu,readonly', '--workdir', '/work', '--entrypoint', 'python3',
                    ref.IMAGE, '/work/entry.py'], check=True, capture_output=True, timeout=20)
    try:
        subprocess.run(['docker', 'start', '--attach', name], check=True, capture_output=True, timeout=600)
    finally:
        subprocess.run(['docker', 'rm', '--force', '--volumes', name], capture_output=True, timeout=30)
    out = []
    for i, p in enumerate(progs):
        d = work / 'jobs' / f'p{i:04d}'
        require((d / 'rc').read_text().strip() == '0', f'QEMU exit status {(d / "rc").read_text()} for {p["name"]}')
        final, fault = ref.parse_output(p, (d / 'qemu.stdout').read_bytes())
        require(fault is None, p['name'])
        und = flags.analyze(p['insns'])
        out.append({'name': p['name'], 'qemu': final, 'disasm': [], 'undefined_flags': ref.hx(und),
                    'rflags_compare_mask': ref.hx(flags.TRACKED & ~und)})
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261102); ap.add_argument('--qemu', type=pathlib.Path)
    ap.add_argument('-n', type=int, default=12)
    a = ap.parse_args()
    raw = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT', HERE.parent.parent.parent / 'natemu-raw'))
    qemu = (a.qemu or raw / 'reference-slice1-20261026-01' / 'pin' / 'qemu-x86_64-static').resolve()
    rng = random.Random(a.seed)
    pairs = [pair(f'o16-branches-{i:03d}', rng.getrandbits(64)) for i in range(a.n)]
    a.out.mkdir(parents=True)
    results = run_qemu([t for _, t in pairs], a.out / 'qemu', qemu)
    a.out.joinpath('programs.json').write_text(json.dumps({'generator': {'script': 'tools/reference/gen_br66.py', 'seed': a.seed},
                                                          'programs': [p for p, _ in pairs]}, indent=1) + '\n')
    a.out.joinpath('expected.json').write_text(json.dumps({'format': ref.FORMAT, 'image': ref.IMAGE, 'qemu_sha256': ref.PINS['qemu-x86_64-static'],
        'qemu_argv': ['/qemu', '-cpu', 'CPU', 'prog.elf'], 'expected_from': 'twin programs: each branch 66 replaced by 3E',
        'results': results}, indent=1) + '\n')
    print(f'{len(pairs)} programs -> {a.out}')

if __name__ == '__main__': main()
