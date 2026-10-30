#!/usr/bin/env python3
"""M1 checks: guest RAM of a run-time size, its top, the hole above it, placement across 0xA0000 (boot-features plan, M1).

    tools/with-env python3 tools/run_m1.py --fexc FEXC --out DIR [--host HOST --runtime RUNTIME]

- `top`: at 4 MiB, 2 GiB and 3 GiB, every table and data page placed in RAM's last MiB (`--place`): a store and a load
  there complete and CR3 (read at CPL 0) lies in that MiB.
- `hole`: the first page above RAM (R, the start of the map's reserved range to 4 GiB) is a device page: an access
  there stops `unsupported_mmio` with status `unassigned` under `--strict-unassigned`, and reads all-ones, counted, without
  it (L3's rule, `hole-rule`); an `mmio` item on RAM's last page is refused by the driver.
- `legacy-around`: placing from 0x9e000 puts the tables at 0x9e000, 0x9f000, then above the legacy hole (CR3 = 0x9e000;
  the run completes).
- `legacy-*` (L3 + M1's patch: 0xA0000-0xFFFFF is not RAM): an unassigned page there reads all-ones (counted), a test
  device registers there, and an identity page there is refused by the driver.
- `xmc-high`: cross-modifying and self-modifying code in a code page near the top of 2 GiB (`--place 0x7ff00000`: the
  code page's mask word and its invalidation-record summary bits are the highest ones). vCPU 1 rewrites the immediate
  of X (`mov esi, 1` -> 2) after a delay, then sets a flag; vCPU 0 runs X in a loop until it sees the flag, executes
  CPUID (serializing) and runs X once more: RSI must be 2. Alone: vCPU 0 runs X, rewrites it and runs it again: RSI 2.
  Block sizes default and 1.

Writes DIR/m1.json; exit status 1 on any failure."""
import argparse, json, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'tools' / 'reference')]
import ref, run_slice1_vm as vm  # noqa: E402

MIB, GIB = 1 << 20, 1 << 30
CODE, X, FLAG, PEER = 0x100000, 0x100800, 0x300000, 0x101000


def gprs(values):
    g = [0] * 16
    for r, v in values.items(): g[r] = v
    return 'gpr ' + ' '.join(f'{v:#x}' for v in g)


def run(a, name, text, extra=()):
    w = a.out / name
    w.mkdir(parents=True, exist_ok=True)
    (w / 'program.txt').write_text(text)
    r = subprocess.run([a.host, a.runtime, 'run-program', a.fexc, w / 'program.txt', w, *(hex(e) if isinstance(e, int) else e for e in extra)], capture_output=True, text=True, timeout=600)
    return json.loads(r.stdout) if r.returncode == 0 else {'stop': 'driver', 'reason': r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ''}


def first(out):
    return out['cpus'][0] if 'cpus' in out else out


def unassigned(out):
    c = first(out)
    return (c['stop'], c['reason'].split()[0], c.get('mmio', {}).get('status')) == ('resident', 'unsupported_mmio', 'unassigned')


def cases(a):
    asm = ref.assemble
    # mov [rbx], rax; mov rcx, [rbx]; mov rax, cr3
    store = asm('mov qword ptr [rbx], rax\nmov rcx, qword ptr [rbx]\nmov rax, cr3').hex()
    load_rdx = asm('mov eax, dword ptr [rdx + 8]').hex()
    other_device = lambda gpa: f'mmio 0x210000 {gpa:#x}\n'  # noqa: E731 (a registered device, so the run reports `mmio`)
    for ram in (4 * MIB, 2 * GIB, 3 * GIB):
        top = run(a, f'top-{ram:#x}', f'rip {CODE:#x}\nrflags 0x2\n{gprs({0: 0x1234, 3: 0x300000})}\ncode {store}\ncpl 0\nregion 0x300000 0x1000\n',
                  ['--ram', ram, '--place', ram - MIB])
        c = first(top)
        yield f'top-{ram:#x}', c['stop'] == 'reached' and int(c['gpr'][1], 0) == 0x1234 and ram - MIB <= int(c['gpr'][0], 0) < ram, c
        text = f'rip {CODE:#x}\nrflags 0x2\n{gprs({2: 0x200000})}\ncode {load_rdx}\nmmio 0x200000 {ram:#x} unassigned\n{other_device(ram + 0x10000)}'
        hole = run(a, f'hole-{ram:#x}', text, ['--ram', ram, '--strict-unassigned'])
        yield f'hole-{ram:#x}', unassigned(hole), first(hole)
        # L3: without the strict switch the page at R reads all-ones (the unassigned rule), counted.
        hole = first(run(a, f'hole-rule-{ram:#x}', text, ['--ram', ram]))
        yield f'hole-rule-{ram:#x}', (hole['stop'] == 'reached' and int(hole['gpr'][0], 0) == 0xffff_ffff
                                     and hole.get('unassigned_pages') == [{'page': f'{ram:#x}', 'reads': 1, 'writes': 0}]), hole
        inside = run(a, f'inside-{ram:#x}', f'rip {CODE:#x}\nrflags 0x2\ncode 90\nmmio 0x200000 {ram - 4096:#x} unassigned\n', ['--ram', ram])
        yield f'inside-{ram:#x}', inside['stop'] == 'driver' and 'is guest RAM' in inside['reason'], inside
    out = run(a, 'legacy-around', f'rip {CODE:#x}\nrflags 0x2\n{gprs({0: 0x1234, 3: 0x300000})}\ncode {store}\ncpl 0\nregion 0x300000 0x1000\n',
              ['--place', 0x9_e000])
    c = first(out)
    yield 'legacy-around', c['stop'] == 'reached' and int(c['gpr'][0], 0) == 0x9_e000 and int(c['gpr'][1], 0) == 0x1234, c
    # L3 + M1: the legacy hole 0xA0000-0xFFFFF is not RAM: an unassigned page there reads all-ones (counted); a test device
    # registers there; an identity page in it is refused.
    out = first(run(a, 'legacy-unassigned', f'rip {CODE:#x}\nrflags 0x2\n{gprs({2: 0x200000})}\ncode {load_rdx}\nmmio 0x200000 0xe0000 unassigned\n'))
    yield 'legacy-unassigned', (out['stop'] == 'reached' and int(out['gpr'][0], 0) == 0xffff_ffff
                                and out.get('unassigned_pages') == [{'page': '0xe0000', 'reads': 1, 'writes': 0}]), out
    out = first(run(a, 'legacy-device', f'rip {CODE:#x}\nrflags 0x2\n{gprs({2: 0x200000})}\ncode {load_rdx}\nmmio 0x200000 0xb8000\nmemory 0x200008 78563412\n'))
    yield 'legacy-device', out['stop'] == 'reached' and int(out['gpr'][0], 0) == 0x1234_5678 and out['devices'][0]['reads'] == [1], out
    out = run(a, 'legacy-identity', f'identity\nrip {CODE:#x}\nrflags 0x2\ncode 90\nmemory 0xa0000 00\n')
    yield 'legacy-identity', out['stop'] == 'driver' and 'legacy hole' in out['reason'], out
    # Cross- and self-modifying code in a code page near the top of 2 GiB.
    x = asm('mov esi, 1\njmp r13').hex()
    c0 = asm(f'start:\nlea r13, [rip + back]\nmov rax, {X:#x}\njmp rax\nback:\ncmp byte ptr [{FLAG:#x}], 1\njne start\n'
             f'xor eax, eax\ncpuid\nlea r13, [rip + done]\nmov rax, {X:#x}\njmp rax\ndone:').hex()
    c1 = asm(f'mov ecx, 200000\nw:\ndec ecx\njnz w\nmov byte ptr [{X + 1:#x}], 2\nmov byte ptr [{FLAG:#x}], 1').hex()
    alone = asm(f'lea r13, [rip + once]\nmov rax, {X:#x}\njmp rax\nonce:\nmov byte ptr [{X + 1:#x}], 2\n'
                f'lea r13, [rip + done]\nmov rax, {X:#x}\njmp rax\ndone:').hex()
    space = f'body {X:#x} {x}\nmemory 0x100f00 00\nregion {FLAG:#x} 0x1000\n'
    for block in (None, 1):
        limit = [str(block)] if block else []
        out = run(a, f'xmc-high-{block}', f'body {CODE:#x} {c0}\nbody {PEER:#x} {c1}\n{space}cpu\nrip {CODE:#x}\nrflags 0x2\ncpl 0\n'
                  f'cpu\nrip {PEER:#x}\nrflags 0x2\ncpl 0\n', [*limit, '--place', 0x7ff0_0000])
        ok = 'cpus' in out and all(c['stop'] == 'reached' for c in out['cpus']) and int(out['cpus'][0]['gpr'][6], 0) == 2
        yield f'xmc-high-{block}', ok, out.get('cpus', out)
        out = run(a, f'smc-high-{block}', f'body {CODE:#x} {alone}\n{space}rip {CODE:#x}\nrflags 0x2\ncpl 0\n', [*limit, '--place', 0x7ff0_0000])
        c = first(out)
        yield f'smc-high-{block}', c['stop'] == 'reached' and int(c['gpr'][6], 0) == 2, c


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--fexc', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--host')
    ap.add_argument('--runtime')
    a = ap.parse_args()
    if not (a.host and a.runtime): a.host, a.runtime = vm.build()
    results, failures = [], []
    for name, ok, detail in cases(a):
        keep = {k: detail.get(k) for k in ('stop', 'reason', 'rip', 'mmio')} if isinstance(detail, dict) else [
            {k: c.get(k) for k in ('stop', 'reason', 'rip')} for c in detail]
        results.append({'case': name, 'ok': ok, 'detail': keep})
        print(f"{name}: {'ok' if ok else 'FAIL'} {'' if ok else keep}", flush=True)
        if not ok: failures.append(name)
    (a.out / 'm1.json').write_text(json.dumps({'results': results, 'failures': failures}, indent=1) + '\n')
    print(f'{len(results)} cases, {len(failures)} failures')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
