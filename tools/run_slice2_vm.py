#!/usr/bin/env python3
"""Control-flow corpus (slice 2, S2d): structured programs run in the VM against pinned QEMU.

    tools/with-env python3 tools/run_slice2_vm.py --fexc FEXC --out DIR [--seed N] [--count 64] [--sizes default 1 2]
    tools/with-env python3 tools/run_slice2_vm.py --fexc FEXC --out DIR --programs P.json --expected E.json

The programs come from tools/reference/gen_slice2.py (seeded; inputs only: bytes, initial state, annotations) or from a
programs file; the expected final states come only from a full pinned-QEMU run of the same bytes (tools/reference/ref.py,
`flow` programs: the flags rule follows the instructions QEMU executed). Each program runs in the VM at each block size
("default" = fexc's own limit, else a limit in guest instructions) and must reach its end with GPRs, masked RFLAGS and
memory equal to QEMU's (the same checks as tools/run_slice1_vm.py). Writes DIR/{programs,expected,results}.json;
exit status 1 on any failure."""
import argparse, json, os, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools' / 'reference'))
import ref, gen_slice2 as gen, run_slice1_vm as vm  # noqa: E402

MAX_EXECUTED = 60_000       # a corpus program is bounded by construction; a run longer than this is a generator defect


def qemu_path():
    raw = pathlib.Path(os.environ.get('NATEMU_RAW_ROOT', ROOT.parent / 'natemu-raw'))
    return raw / 'reference-slice1-20261026-01' / 'pin' / 'qemu-x86_64-static'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--seed', type=int, default=20261027)
    ap.add_argument('--count', type=int, default=64)
    ap.add_argument('--sizes', nargs='+', default=['default', '1', '2'])
    ap.add_argument('--programs', type=pathlib.Path)
    ap.add_argument('--expected', type=pathlib.Path)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if a.programs:
        programs = json.loads(a.programs.read_text())['programs']
    else:
        programs = gen.corpus(a.seed, a.count)
    if a.expected:
        results = json.loads(a.expected.read_text())['results']
    else:
        results, prov = ref.run_programs(programs, a.out / 'ref', qemu_path())
        (a.out / 'expected.json').write_text(json.dumps({**prov, 'results': results}, indent=1) + '\n')
    (a.out / 'programs.json').write_text(json.dumps({'programs': programs}, indent=1) + '\n')
    expected = {r['name']: r for r in results}
    too_long = [r['name'] for r in results if r.get('executed_insns', 0) > MAX_EXECUTED]
    assert not too_long, f'programs over {MAX_EXECUTED} executed instructions: {too_long}'
    print(f"{len(programs)} programs; executed instructions {min(r.get('executed_insns', 0) for r in results)}.."
          f"{max(r.get('executed_insns', 0) for r in results)}", flush=True)
    host, runtime = vm.build()
    out, failures = [], 0
    for size in a.sizes:
        counts = {'match': 0, 'unsupported': 0, 'fail': 0}
        for p in programs:
            entry = vm.run_program(host, runtime, a.fexc, p, expected[p['name']], a.out / 'work' / size / p['name'], None if size == 'default' else int(size))
            entry['size'] = size
            counts[entry['outcome']] += 1
            out.append(entry)
            stats = entry.get('vm', {}).get('stats', {})
            if entry['outcome'] != 'match' or size == a.sizes[0]:
                print(f"[{size}] {p['name']}: {entry['outcome']} blocks={stats.get('blocks')} links={stats.get('links')} "
                      f"{'; '.join(entry.get('diffs', []))[:300]} {entry['stderr'].strip()[-300:]}", flush=True)
        print(f'[{size}] {counts}', flush=True)
        failures += counts['fail'] + counts['unsupported']
    (a.out / 'results.json').write_text(json.dumps({'results': out}, indent=1) + '\n')
    sys.exit(1 if failures else 0)


if __name__ == '__main__': main()
