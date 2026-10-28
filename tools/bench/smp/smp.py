#!/usr/bin/env python3
"""Multi-CPU QEMU system-mode reference: image builder, runner, dump parser, comparer. See README.md.

    python3 tools/bench/smp/smp.py selftest --out DIR
    python3 tools/bench/smp/smp.py det      --out DIR [--reps 5] [--cpus 2,3,4] [--programs a,b] [--modes multi,single]
    python3 tools/bench/smp/smp.py litmus   --out DIR [--runs 20] [--k 32768] [--tests sb,mp] [--single-runs 3 --single-k 4096]
    python3 tools/bench/smp/smp.py build    [--k 32768]       # (re)write programs.json and litmus.json next to this file
"""
import argparse, collections, hashlib, json, pathlib, shutil, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE.parent.parent))
QEMU = shutil.which('qemu-system-x86_64') or '/opt/homebrew/bin/qemu-system-x86_64'
CFG, RESULT, CAPTURE, LOW, HIGH = 0x106000, 0x107000, 0x100800, 0x110000, 0x400000
FLAG_MASK = 0x8d5
# `system` programs (CPL0 bodies with system instructions) run on natemu's CPU model: QEMU's max CPU without PCID and
# SMAP (CR4.PCIDE and CR4.SMAP are reserved then), and dump a fault record per CPU (40 bytes at slot + 0xa0).
SYSTEM_CPU = 'max,-pcid,-smap'
FAULT = 0xa0
M64 = (1 << 64) - 1
REGS = ('rax', 'rcx', 'rdx', 'rbx', 'rsp', 'rbp', 'rsi', 'rdi', 'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15')


def num(x): return int(x, 0) if isinstance(x, str) else x

def lcg_bytes(size, seed):
    x, out = seed, bytearray()
    for _ in range(size):
        x = (x * 6364136223846793005 + 1442695040888963407) & M64
        out.append(x >> 56)
    return bytes(out)

def region_bytes(r):
    """Initial contents of a memory region: inline hex `data`, or `size` + `fill` zero|lcg(seed)."""
    if 'data' in r: return bytes.fromhex(r['data'])
    return bytes(r['size']) if r['fill'] == 'zero' else lcg_bytes(r['size'], r['seed'])

def regions(prog): return [(num(r['addr']), len(region_bytes(r)), r.get('capture', True), r) for r in prog['memory']]


def build_stub(workdir):
    workdir.mkdir(parents=True, exist_ok=True); out = workdir / 'stub_smp.bin'
    subprocess.run(['nasm', '-f', 'bin', str(HERE / 'stub_smp.asm'), '-o', str(out)], check=True)
    stub = out.read_bytes(); assert len(stub) == LOW - 0x100000 and stub[0x6000:0x6008] == b'SMPCFG01'
    return stub


def build_image(prog, stub):
    """Flat Multiboot image: stub + bodies (each followed by `jmp capture`) + initial memory + config (cpu table, dump table)."""
    n = prog['ncpu']; assert 1 <= n <= 4 and len(prog['cpus']) == n
    img = bytearray(stub); used = []                         # image offset 0 = address 0x100000
    def put(addr, data, what):
        assert LOW <= addr and addr + len(data) <= HIGH, f'{what} outside {LOW:#x}..{HIGH:#x}'
        for a, b, w in used: assert addr + len(data) <= a or b <= addr, f'{what} overlaps {w}'
        used.append((addr, addr + len(data), what))
        off = addr - 0x100000
        if len(img) < off + len(data): img.extend(bytes(off + len(data) - len(img)))
        img[off:off + len(data)] = data
    ends = []
    for b in prog['bodies']:
        a, code = num(b['addr']), bytes.fromhex(b['code'])
        put(a, code + b'\xe9' + (CAPTURE - (a + len(code) + 5)).to_bytes(4, 'little', signed=True), 'body')
        ends.append((a, a + len(code)))
    for a, size, _, r in regions(prog):
        put(a, region_bytes(r), 'memory')                    # zero regions too: RAM beyond the image is NOT zero (QEMU/SeaBIOS leave the kernel path at 0x250000)
    def cfg(off, data): img[CFG - 0x100000 + off:CFG - 0x100000 + off + len(data)] = data
    cfg(8, n.to_bytes(8, 'little')); cfg(0x10, b'ENDDUMP!')
    for i, c in enumerate(prog['cpus']):
        g = [num(x) for x in c['gpr']]; assert len(g) == 16
        rip = num(c['rip']); assert any(a <= rip < e for a, e in ends), f'cpu {i} rip {rip:#x} not inside a body'
        assert num(c.get('rflags', 0)) & ~FLAG_MASK == 0
        assert c.get('cpl', 3) in (0, 3)
        cfg(0x800 + 0x100 * i, b''.join(x.to_bytes(8, 'little') for x in g) + rip.to_bytes(8, 'little') + num(c.get('rflags', 0)).to_bytes(8, 'little')
            + c.get('cpl', 3).to_bytes(8, 'little'))
    faults = [(RESULT + 0x100 * i + FAULT, 40) for i in range(n)] if prog.get('system') else []
    segs = [(CFG, 16)] + [(RESULT + 0x100 * i, 136) for i in range(n)] + faults + [(a, s) for a, s, cap, _ in regions(prog) if cap] + [(CFG + 0x10, 8)]
    assert len(segs) < 64
    cfg(0x100, b''.join(a.to_bytes(8, 'little') + s.to_bytes(8, 'little') for a, s in segs))
    return bytes(img)


def qemu_args(n, mode, dbg, img, cpu='max'):
    return [QEMU, '-accel', f'tcg,thread={mode}', '-cpu', cpu, '-smp', str(n), '-m', '64', '-machine', 'pc', '-nographic', '-nodefaults',
            '-monitor', 'none', '-serial', 'none', '-device', 'isa-debug-exit,iobase=0xf4,iosize=0x04', '-no-reboot',
            '-debugcon', f'file:{dbg}', '-kernel', str(img)]


def run_image(prog, img, work, mode='multi', timeout=120):
    work.mkdir(parents=True, exist_ok=True); p, dbg = work / 'image.bin', work / 'dbg.out'
    p.write_bytes(img); dbg.unlink(missing_ok=True); t0 = time.perf_counter()
    try:
        cpu = SYSTEM_CPU if prog.get('system') else 'max'
        r = subprocess.run(qemu_args(prog['ncpu'], mode, dbg, p, cpu), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
        rc, err = r.returncode, r.stderr.decode(errors='replace')[-300:]
    except subprocess.TimeoutExpired:
        rc, err = 'timeout', ''
    return rc, err, (dbg.read_bytes() if dbg.exists() else b''), time.perf_counter() - t0


def parse_dump(prog, data):
    """-> {'cpus': [{'gpr': [16 ints], 'rflags': int}], 'memory': [{'addr','data'}...captured regions in program order]}; a
    `system` program's CPUs also have 'fault': None or (vector, error code, RIP, CR2 (#PF only, else 0)). Raises ValueError."""
    n = prog['ncpu']; caps = [(a, s) for a, s, c, _ in regions(prog) if c]
    system = bool(prog.get('system'))
    want = 16 + 136 * n + 40 * n * system + sum(s for _, s in caps) + 8
    if len(data) != want: raise ValueError(f'dump is {len(data)} bytes, expected {want}')
    if data[:8] != b'SMPCFG01' or int.from_bytes(data[8:16], 'little') != n or data[-8:] != b'ENDDUMP!': raise ValueError('bad dump header/footer')
    cpus, o = [], 16
    for i in range(n):
        q = [int.from_bytes(data[o + 8 * j:o + 8 * j + 8], 'little') for j in range(17)]
        cpus.append({'gpr': q[:16], 'rflags': q[16]}); o += 136
    for c in cpus if system else []:
        faulted, vector, error, rip, cr2 = (int.from_bytes(data[o + 8 * j:o + 8 * j + 8], 'little') for j in range(5)); o += 40
        c['fault'] = (vector, error, rip, cr2 if vector == 14 else 0) if faulted else None
    mem = []
    for a, s in caps: mem.append({'addr': a, 'data': data[o:o + s]}); o += s
    return {'cpus': cpus, 'memory': mem}


def compare(prog, got, want):
    """Differences (list of strings) between two parsed states; RFLAGS only under FLAG_MASK. `want` has the same shape."""
    d = []
    for i, (g, w) in enumerate(zip(got['cpus'], want['cpus'])):
        for k, (x, y) in enumerate(zip(g['gpr'], w['gpr'])):
            if x != y: d.append(f'cpu{i} {REGS[k]}: {x:#x} != {y:#x}')
        if (g['rflags'] ^ w['rflags']) & FLAG_MASK: d.append(f"cpu{i} rflags: {g['rflags'] & FLAG_MASK:#x} != {w['rflags'] & FLAG_MASK:#x}")
        if g.get('fault') != w.get('fault'):
            f = lambda x: x and '(vector %d, error %#x, rip %#x, cr2 %#x)' % x
            d.append(f"cpu{i} fault: {f(g.get('fault'))} != {f(w.get('fault'))}")
    if len(got['memory']) != len(want['memory']): d.append('memory region count')
    for g, w in zip(got['memory'], want['memory']):
        if g['addr'] != w['addr'] or len(g['data']) != len(w['data']): d.append(f"region {g['addr']:#x} shape")
        elif g['data'] != w['data']:
            j = next(k for k in range(len(g['data'])) if g['data'][k] != w['data'][k])
            d.append(f"region {g['addr']:#x}: first difference at +{j:#x}: {g['data'][j]:#04x} != {w['data'][j]:#04x} ({sum(a != b for a, b in zip(g['data'], w['data']))} bytes differ)")
    return d


def manifest(out):
    info = lambda *c: subprocess.run(c, capture_output=True, text=True).stdout.strip()
    m = {'qemu': str(pathlib.Path(QEMU).resolve()), 'version': info(QEMU, '--version').splitlines()[0],
         'sha256': hashlib.sha256(pathlib.Path(QEMU).resolve().read_bytes()).hexdigest(), 'nasm': info('nasm', '-v'),
         'host': info('sysctl', '-n', 'machdep.cpu.brand_string'), 'os': info('sw_vers', '-productVersion'), 'ncpu': info('sysctl', '-n', 'hw.ncpu'),
         'stub_asm_sha256': hashlib.sha256((HERE / 'stub_smp.asm').read_bytes()).hexdigest(),
         'args_template': qemu_args('N', 'multi|single', 'DBG', 'IMAGE')[1:], 'time': time.strftime('%Y-%m-%dT%H:%M:%S')}
    (out / 'manifest.json').write_text(json.dumps(m, indent=1) + '\n'); return m


def load(name): return json.loads((HERE / name).read_text())


# ---- commands -------------------------------------------------------------------------------------------------------
def cmd_build(a):
    import programs
    (HERE / 'programs.json').write_text(json.dumps(programs.det_all(), indent=1) + '\n')
    (HERE / 'litmus.json').write_text(json.dumps(programs.litmus_all(a.k), indent=1) + '\n')
    (HERE / 'system.json').write_text(json.dumps(programs.system_all(), indent=1) + '\n')


def det_one(prog, stub, work, mode, reps, log, model):
    want = model.expected(prog); digests, fails, walls = collections.Counter(), [], []
    img = build_image(prog, stub)
    for r in range(reps):
        rc, err, data, dt = run_image(prog, img, work, mode); walls.append(dt)
        try:
            got = parse_dump(prog, data); diff = compare(prog, got, want) if rc == 1 else [f'exit code {rc}: {err}']
        except ValueError as e:
            diff = [f'exit code {rc}: {e}']
        digests[hashlib.sha256(data).hexdigest()[:16]] += 1
        if diff: fails.append({'rep': r, 'diff': diff[:5]})
    return {'reps': reps, 'pass': reps - len(fails), 'distinct_dumps': len(digests), 'dump_digests': dict(digests), 'failures': fails[:3], 'wall_s_median': sorted(walls)[len(walls) // 2]}


def cmd_det(a):
    import model_smp as model
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True); manifest(out); stub = build_stub(out / 'build'); logf = open(out / 'det.log', 'a')
    def log(s): print(s, flush=True); logf.write(s + '\n'); logf.flush()
    progs = load('programs.json'); res = {}; bad = 0
    for p in progs:
        if a.programs and p['name'].split('@')[0] not in a.programs.split(','): continue
        if str(p['ncpu']) not in a.cpus.split(','): continue
        for mode in a.modes.split(','):
            r = det_one(p, stub, out / 'run', mode, a.reps, log, model); res[f"{p['name']}/{mode}"] = r
            ok = r['pass'] == r['reps'] and r['distinct_dumps'] == 1; bad += not ok
            log(f"{p['name']:24} -smp {p['ncpu']} {mode:6} model-match {r['pass']}/{r['reps']}  distinct dumps {r['distinct_dumps']}  median wall {r['wall_s_median']:.2f}s  {'OK' if ok else 'FAIL ' + json.dumps(r['failures'])}")
    (out / 'det-results.json').write_text(json.dumps(res, indent=1) + '\n')
    if bad: raise SystemExit(f'{bad} configuration(s) failed')


def cmd_selftest(a):
    import model_smp as model
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True); stub = build_stub(out / 'build'); res = {}
    prog = next(p for p in load('programs.json') if p['name'] == 'xchg-lock@2')
    img = build_image(prog, stub); rc, err, data, _ = run_image(prog, img, out / 'run', 'multi'); want = model.expected(prog); got = parse_dump(prog, data)
    res['clean'] = {'rc': rc, 'diff': compare(prog, got, want)}; assert rc == 1 and not res['clean']['diff'], res
    (out / 'xchg-lock@2.dump').write_bytes(data)
    # corrupted-dump controls: every one must be reported
    ctl = {}
    for name, mut in (('flip_gpr_bit', lambda b: b.__setitem__(16 + 8 * 3, b[16 + 8 * 3] ^ 1)),                     # cpu0 rbx bit 0
                      ('flip_cpu1_gpr', lambda b: b.__setitem__(16 + 136 + 8 * 8, b[16 + 136 + 8 * 8] ^ 0x80)),        # cpu1 r8
                      ('flip_flags', lambda b: b.__setitem__(16 + 128, b[16 + 128] ^ 0x40)),                           # cpu0 ZF
                      ('flip_counter', lambda b: b.__setitem__(16 + 272 + 0x80, b[16 + 272 + 0x80] ^ 1))):             # shared counter byte
        c = bytearray(data); mut(c)
        d = compare(prog, parse_dump(prog, bytes(c)), want); ctl[name] = d; assert d, f'control {name} was not detected'
    for name, c in (('truncated', data[:-1]), ('bad_footer', data[:-1] + b'?'), ('extra_byte', data + b'\0')):
        try: parse_dump(prog, c); raise AssertionError(f'control {name} was accepted')
        except ValueError as e: ctl[name] = str(e)
    res['controls'] = ctl
    # a CPU that produces a different answer: change a per-CPU register and expect the model to disagree
    p2 = json.loads(json.dumps(prog)); p2['cpus'][1]['gpr'][1] = hex(int(p2['cpus'][1]['gpr'][1], 0) - 1)            # one fewer iteration on cpu 1
    rc2, _, d2, _ = run_image(p2, build_image(p2, stub), out / 'run', 'multi'); res['wrong_input'] = compare(p2, parse_dump(p2, d2), want); assert res['wrong_input']
    (out / 'selftest.json').write_text(json.dumps(res, indent=1, default=str) + '\n'); print(json.dumps(res, indent=1, default=str)); print('selftest OK')


def cmd_litmus(a):
    import programs
    from i4v_ordering_model import enumerate_outcomes
    out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True); manifest(out); stub = build_stub(out / 'build'); logf = open(out / 'litmus.log', 'a')
    def log(s): print(s, flush=True); logf.write(s + '\n'); logf.flush()
    allowed_cache, res = {}, {}
    for t in programs.LITMUS_NAMES:
        if a.tests and t not in a.tests.split(','): continue
        for mode, runs, k in (('multi', a.runs, a.k), ('single', a.single_runs, a.single_k)):
            if not runs: continue
            prog, test = programs.litmus(t, k, pause=(mode == 'single'))
            allowed = allowed_cache.setdefault(t, enumerate_outcomes(test).outcomes); hist = collections.Counter(); walls = []
            img = build_image(prog, stub)
            for r in range(runs):
                rc, err, data, dt = run_image(prog, img, out / 'run', mode, timeout=300); walls.append(dt)
                if rc != 1: raise SystemExit(f'{t} {mode}: exit {rc} {err}')
                got = parse_dump(prog, data); hist.update(programs.litmus_outcomes(prog, test, got, k))
            outside = {o: c for o, c in hist.items() if o not in allowed}
            res[f'{t}/{mode}'] = {'cpus': prog['ncpu'], 'runs': runs, 'k': k, 'trials': runs * k, 'pause_in_spins': mode == 'single',
                                  'outcomes': {','.join(map(str, o)): c for o, c in sorted(hist.items())},
                                  'model_allowed': [','.join(map(str, o)) for o in sorted(allowed)], 'observed_outside_model': {','.join(map(str, o)): c for o, c in outside.items()},
                                  'model_allowed_not_observed': [','.join(map(str, o)) for o in sorted(allowed) if o not in hist], 'wall_s_median': sorted(walls)[len(walls) // 2]}
            log(f"{t:10} {mode:6} smp {prog['cpus'].__len__()} trials {runs * k:8}  observed {dict(sorted(hist.items()))}  outside-model {outside or 'none'}  unseen-allowed {res[f'{t}/{mode}']['model_allowed_not_observed']}")
    (out / 'litmus-results.json').write_text(json.dumps(res, indent=1) + '\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__); sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('build'); s.add_argument('--k', type=int, default=32768); s.set_defaults(f=cmd_build)
    s = sub.add_parser('selftest'); s.add_argument('--out', required=True); s.set_defaults(f=cmd_selftest)
    s = sub.add_parser('det'); s.add_argument('--out', required=True); s.add_argument('--reps', type=int, default=5)
    s.add_argument('--cpus', default='2,3,4'); s.add_argument('--programs'); s.add_argument('--modes', default='multi,single'); s.set_defaults(f=cmd_det)
    s = sub.add_parser('litmus'); s.add_argument('--out', required=True); s.add_argument('--runs', type=int, default=20); s.add_argument('--k', type=int, default=32768)
    s.add_argument('--single-runs', type=int, default=3); s.add_argument('--single-k', type=int, default=4096); s.add_argument('--tests'); s.set_defaults(f=cmd_litmus)
    a = ap.parse_args(); a.f(a)

if __name__ == '__main__':
    main()
