#!/usr/bin/env python3
"""The shadow-table pool's full reset on a real guest program (`run-program --shadow-pool-pages`).

    tests/system/run_shadow_pool.py --fexc FEXC --out DIR [--host HOST --runtime RUNTIME]

Since November 25 each vCPU's pool of shadow tables is a 16,384-page reservation (`natemu_abi::shadow::POOL_MAX_PAGES`),
which ordinary guests never fill, so the path that runs when a pool is full (every shadow of the vCPU reset, counted as
`evictions`, the access retried: the resident's `install`) would go untested. These programs fill a pool of the least
size a launch may set (285 pages, `POOL_MIN_PAGES`) for certain: they touch one page in each of 300 regions 1 GiB
apart, then read every page back. Each such page needs a level-2 and a leaf table of its own in the shadow tables (two
pool pages), so the 300 pages need about 600: every pass fills the pool twice or more.

- `user-low`: CPL 3, RAM pages at 1 GiB, 2 GiB, ... 300 GiB, stored then read (the user space's tables, from the bottom of
  the pool): the full-pool branch of the resident's `install`.
- `kernel-high`: the same at CPL 0 from 0xffff800040000000 (the supervisor space's upper half, the "kept half" whose
  tables come from the top of the pool).
- `unassigned`: CPL 3, the 300 pages all map one guest-physical page above RAM with no device (L3's rule: shadowed
  read-only onto the all-ones page), read only, each read all-ones: `install_ones`.
- `rom`: the same onto the firmware image's page (`firmware`, one page at 4 GiB - 4 KiB), each read its first word:
  `install_rom`.
- `mapped`: the 300 pages map 300 pages of a mapped device-memory region (`--mapped-at`), stored then read:
  `install_mapped`.

Each runs at block sizes default and 1, with `--shadow-pool-pages 285` (pass: the program reaches its end, every page
read back holds what it should, RDX = 0 mismatches and RBX = the sum of the values, and `evictions` > 0) and
with the default pool (the same results and `evictions` = 0: the workload fits the reservation, so the evictions above
come from the small pool). Writes DIR/shadow-pool.json and DIR/results.json; exit status 1 on any failure."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse, json, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference')]
import ref, run_slice1_vm as vm  # noqa: E402

GIB, CODE, N, SMALL = 1 << 30, 0x100000, 300, 285
ROM, ROM_WORD = 0xffff_f000, 0x1122_3344_5566_7788   # the firmware image's one page (4 GiB - 4 KiB) and its first word
UNASSIGNED = 0xc000_0000                             # above the 2 GiB of RAM, in the map's reserved range: no device
MAPPED, MAPPED_LEN = 0xd000_0000, 0x20_0000          # a mapped device-memory region (`--mapped-at`)


def code(base, store, expect):
    """`store`: each page's address stored into it first. Then every page is read back: RDX counts the reads that differ
    from `expect` (None: the page's own address), RBX sums what was read."""
    first = f"""
        mov ecx, {N}
        movabs rdi, {base:#x}
        movabs rsi, {GIB:#x}
    store:
        mov qword ptr [rdi], rdi
        add rdi, rsi
        dec rcx
        jnz store""" if store else ''
    want = 'rdi' if expect is None else 'r8'
    return ref.assemble(f"""{first}
        mov ecx, {N}
        movabs rdi, {base:#x}
        movabs rsi, {GIB:#x}
        movabs r8, {(expect or 0):#x}
        xor edx, edx
        xor ebx, ebx
    load:
        mov rax, qword ptr [rdi]
        cmp rax, {want}
        je same
        inc rdx
    same:
        add rbx, rax
        add rdi, rsi
        dec rcx
        jnz load""").hex()


def program(base, cpl, pages, store=True, expect=None, head=''):
    """The program at `cpl` over N pages at `base` + i GiB, each described by `pages(i, va)` (a description line)."""
    lines = ''.join(pages(i, base + i * GIB) for i in range(N))
    return f'{head}rip {CODE:#x}\nrflags 0x2\ncpl {cpl}\ncode {code(base, store, expect)}\n{lines}'


def ram(i, va): return f'region {va:#x} 0x1000\n'


# name: (description, the value every read gives (None: the page's address), extra driver arguments, the resident's
# install path whose full-pool branch the small pool takes).
CASES = {
    'user-low': (program(GIB, 3, ram), None, [], 'install (RAM)'),
    'kernel-high': (program(0xffff_8000_4000_0000, 0, ram), None, [], 'install (RAM, the kept half)'),
    'unassigned': (program(GIB, 3, lambda i, va: f'mmio {va:#x} {UNASSIGNED:#x} unassigned\n', store=False, expect=(1 << 64) - 1),
                   (1 << 64) - 1, [], 'install_ones'),
    'rom': (program(GIB, 3, lambda i, va: f'mmio {va:#x} {ROM:#x} unassigned\n', store=False, expect=ROM_WORD,
                    head=f"firmware {ROM_WORD.to_bytes(8, 'little').hex() + '00' * 4088}\n"), ROM_WORD, [], 'install_rom'),
    'mapped': (program(GIB, 3, lambda i, va: f'mmio {va:#x} {MAPPED + (i % (MAPPED_LEN // 4096)) * 4096:#x} unassigned\n'),
               None, ['--mapped-at', f'{MAPPED:#x},{MAPPED_LEN:#x}'], 'install_mapped'),
}


def run(a, name, text, extra):
    w = a.out / name
    w.mkdir(parents=True, exist_ok=True)
    (w / 'program.txt').write_text(text)
    r = subprocess.run([a.host, a.runtime, 'run-program', a.fexc, w / 'program.txt', w, *extra], capture_output=True, text=True, timeout=600)
    if r.returncode:
        return {'stop': 'driver', 'reason': r.stderr.strip().splitlines()[-1] if r.stderr.strip() else f'exit {r.returncode}'}
    out = json.loads(r.stdout)
    return out['cpus'][0] if 'cpus' in out else out


def cases(a):
    for label, (text, value, extra, path) in CASES.items():
        base = 0xffff_8000_4000_0000 if label == 'kernel-high' else GIB
        reads = [value if value is not None else base + i * GIB for i in range(N)]
        want_sum = sum(reads) % (1 << 64)
        for block in (None, 1):
            limit = [str(block)] if block else []
            for pool, evicts in ((SMALL, True), (None, False)):
                name = f"{label}-{'small' if pool else 'default'}{'-1' if block else ''}"
                c = run(a, name, text, [*limit, *extra, *(['--shadow-pool-pages', str(pool)] if pool else [])])
                problems = []
                if c.get('stop') != 'reached':
                    problems.append(f"stopped {c.get('stop')} {c.get('reason')} at {c.get('rip')}")
                else:
                    gpr = [int(v, 0) for v in c['gpr']]
                    if gpr[2] != 0: problems.append(f'{gpr[2]} pages read back another value')
                    if gpr[3] != want_sum: problems.append(f'sum {gpr[3]:#x} != {want_sum:#x}')
                    evictions = c['stats']['evictions']
                    if evicts and evictions == 0: problems.append(f'no eviction with a pool of 285 pages ({path})')
                    if not evicts and evictions != 0: problems.append(f'{evictions} evictions with the default pool')
                yield name, problems, {k: c.get(k) for k in ('stop', 'reason', 'rip')} | {'evictions': c.get('stats', {}).get('evictions')}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--host')
    ap.add_argument('--runtime')
    a = ap.parse_args()
    if not (a.host and a.runtime): a.host, a.runtime = vm.build()
    results = []
    for name, problems, detail in cases(a):
        results.append({'case': name, 'problems': problems, 'detail': detail})
        print(f"{name}: {'ok' if not problems else 'FAIL ' + '; '.join(problems)} {detail}", flush=True)
    (a.out / 'shadow-pool.json').write_text(json.dumps({'results': results}, indent=1) + '\n')
    (a.out / 'results.json').write_text(json.dumps({'results': [
        {'name': r['case'], 'outcome': 'fail' if r['problems'] else 'match', 'diffs': r['problems']} for r in results]}, indent=1) + '\n')
    failures = [r['case'] for r in results if r['problems']]
    print(f'{len(results)} cases, {len(failures)} failures')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    main()
