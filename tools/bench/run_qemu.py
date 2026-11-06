#!/usr/bin/env python3
"""QEMU TCG baseline for natemu: steady-state ns per guest instruction of the benchmarks in benchmarks.json.

    python3 tools/bench/run_qemu.py --out /Users/liaozping/orca/natemu-raw/bench-qemu-20261027-01 [--reps 7] [--scale 1.0]
                                    [--bench int,mem64] [--mults 1,2,4] [--no-linux-user] [--no-system]

System TCG (binding yardstick): the Homebrew qemu-system-x86_64 on this Mac boots stub.asm (long mode, 4 KiB identity paging, CPL3)
and runs the body at N, 2N, 4N iterations (interleaved over repetitions). Linux-user (reference only): the same bytes as a
tools/reference ELF under the pinned container's QEMU 7.2. Slope of wall time against exact dynamic instruction count (native C
model) cancels startup. Every run's final GPRs are compared with the model, so a broken benchmark cannot look fast."""
import argparse, datetime, hashlib, json, os, pathlib, platform, shutil, statistics, subprocess, sys, time, uuid

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / 'reference')); sys.path.insert(0, str(HERE.parent))
import build_bench as bb
QEMU_SYS = shutil.which('qemu-system-x86_64') or '/opt/homebrew/bin/qemu-system-x86_64'
QEMU_USER = pathlib.Path('/Users/liaozping/orca/natemu-raw/reference-slice1-20261026-01/pin/qemu-x86_64-static')
SYS_ARGS = ['-accel', 'tcg,thread=single', '-cpu', 'max', '-smp', '1', '-m', '64', '-machine', 'pc', '-nographic', '-nodefaults',
            '-monitor', 'none', '-serial', 'none', '-device', 'isa-debug-exit,iobase=0xf4,iosize=0x04', '-no-reboot']
USER_FLAGS = ['-cpu', 'max']
INIT_RCX = 0x7800 + 8        # offset of the rcx slot of the initial-GPR block inside the image


def sh(cmd, **kw):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, **kw)
    return (r.stdout + r.stderr).strip()

def sha(p): return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()

def host_state():
    return {'time': datetime.datetime.now().isoformat(timespec='seconds'), 'uptime': sh('uptime'),
            'cpu_usage': sh("top -l 2 -n 0 -s 1 | grep 'CPU usage' | tail -1"), 'thermal': sh('pmset -g therm | head -3'),
            'power': sh('pmset -g batt | head -2'), 'top_cpu': sh('ps -Ao pcpu,comm -r | head -6')}

def manifest(out):
    info = json.loads(sh(['brew', 'info', '--json=v2', 'qemu']))['formulae'][0]
    inst = info['installed'][0]
    m = {'qemu_system': {'path': str(pathlib.Path(QEMU_SYS).resolve()), 'sha256': sha(QEMU_SYS), 'version': sh([QEMU_SYS, '--version']).splitlines()[0],
                         'brew_version': info['versions']['stable'], 'bottle_rebuild': inst.get('bottle_rebuild'), 'installed_with_bottle': inst.get('poured_from_bottle'),
                         'installed_size_mib': round(int(sh(f"du -sk {pathlib.Path(QEMU_SYS).resolve().parents[1]}").split()[0]) / 1024, 1),
                         'runtime_dependencies': inst.get('runtime_dependencies')},
         'qemu_user': {'path': str(QEMU_USER), 'sha256': sha(QEMU_USER), 'note': 'Debian qemu-user-static 1:7.2+dfsg-7+deb12u18+b3 arm64, in the pinned image used by tools/reference/ref.py'},
         'sys_args': SYS_ARGS, 'user_flags': USER_FLAGS,
         'host': {'chip': sh('sysctl -n machdep.cpu.brand_string'), 'os': sh('sw_vers -productVersion'), 'python': platform.python_version(),
                  'nasm': sh('nasm -v'), 'cc': sh('cc --version | head -1'), 'ncpu': os.cpu_count()},
         'benchmarks_json_sha256': sha(HERE / 'benchmarks.json'), 'stub_asm_sha256': sha(HERE / 'stub.asm')}
    (out / 'manifest.json').write_text(json.dumps(m, indent=1) + '\n')
    return m


def build_images(out, benches, mults, scale):
    """images/<bench>-n<N>.bin: stub + body with rcx=N patched into the initial-GPR block."""
    d = out / 'images'; d.mkdir(exist_ok=True); ns = {}
    for b in benches:
        w = out / 'build' / b['name']; w.mkdir(parents=True, exist_ok=True)
        (w / 'body.bin').write_bytes(bytes.fromhex(b['code']))
        (w / 'init.bin').write_bytes(b''.join(g.to_bytes(8, 'little') for g in bb.gprs(b, 0)))
        (w / 'buf.bin').write_bytes(bb.fill_bytes(bb.REGION['buffer']))
        base = w / 'image.bin'
        subprocess.run(['nasm', '-f', 'bin', '-DBODY="body.bin"', '-DINIT="init.bin"', '-DBUF="buf.bin"', str(HERE / 'stub.asm'), '-o', str(base)], check=True, cwd=w)
        img = bytearray(base.read_bytes()); assert len(img) == 0x20000 and 0x100000 + len(b['code']) // 2 + 200 < 0x101000
        for m in mults:
            n = max(1, int(b['n_default'] * scale)) * m; ns[(b['name'], m)] = n
            img[INIT_RCX:INIT_RCX + 8] = n.to_bytes(8, 'little')
            (d / f"{b['name']}-n{n}.bin").write_bytes(img)
    return ns


def run_system(out, benches, mults, ns, reps, models, log):
    res = {b['name']: [] for b in benches}
    for rep in range(-1, reps):                       # rep -1 is a discarded warm-up
        for b in benches:
            row = []
            for m in mults:
                n = ns[(b['name'], m)]; dbg = out / 'run' / 'dbg.out'; dbg.parent.mkdir(exist_ok=True); dbg.unlink(missing_ok=True)
                t0 = time.perf_counter()
                r = subprocess.run([QEMU_SYS, *SYS_ARGS, '-debugcon', f'file:{dbg}', '-kernel', str(out / 'images' / f"{b['name']}-n{n}.bin")],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900)
                dt = time.perf_counter() - t0
                data = dbg.read_bytes() if dbg.exists() else b''
                got = [int.from_bytes(data[i:i + 8], 'little') for i in range(0, len(data), 8)]
                if r.returncode != 1 or got != models[(b['name'], n)][0]:
                    raise SystemExit(f"FAIL system {b['name']} N={n}: rc={r.returncode} stderr={r.stderr[-200:]!r}\n got {[hex(x) for x in got]}\n want {[hex(x) for x in models[(b['name'], n)][0]]}")
                row.append(dt)
            if rep >= 0: res[b['name']].append({'rep': rep, 't': row, 'loadavg': os.getloadavg()[0]})
            log(f'system rep {rep} {b["name"]}: ' + ' '.join(f'{x:.3f}' for x in row))
    return res


def put_elf(path, data): path.write_bytes(data); path.chmod(0o755)

def run_user(out, benches, mults, ns, reps, models, log):
    from ref import norm, build_elf, parse_output
    w = out / 'lu'; (w / 'elfs').mkdir(parents=True, exist_ok=True); shutil.rmtree(w / 'out', ignore_errors=True)
    progs, runs, counts = {}, [], []
    for b in benches:
        for m in mults:
            n = ns[(b['name'], m)]; p = norm(bb.program(b, n)); progs[(b['name'], m)] = p
            put_elf(w / 'elfs' / f"{b['name']}-m{m}.elf", build_elf(p)[0])
        for c in (2, 3):
            put_elf(w / 'elfs' / f"{b['name']}-c{c}.elf", build_elf(norm(bb.program(b, c)))[0])
            counts.append({'id': f"{b['name']}-c{c}", 'elf': f"elfs/{b['name']}-c{c}.elf"})
    for rep in range(-1, reps):
        for b in benches:
            for m in mults:
                runs.append({'id': f"{b['name']}-m{m}-r{rep}", 'elf': f"elfs/{b['name']}-m{m}.elf", 'keep': rep == 0})
    (w / 'jobs.json').write_text(json.dumps({'flags': USER_FLAGS, 'runs': runs, 'counts': counts}))
    shutil.copy2(HERE / 'lu_entry.py', w / 'lu_entry.py'); (w / 'results.json').unlink(missing_ok=True)
    assert sha(QEMU_USER) == bb_pins()['qemu-x86_64-static']
    token = uuid.uuid4().hex; name = 'natemu-bench-' + token; image = bb_image()
    args = ['docker', 'create', '--name', name, '--label', 'natemu.qemu.token=' + token, '--network', 'none', '--read-only',
            '--cpus', '2', '--memory', '512m', '--pids-limit', '128', '--tmpfs', '/tmp:rw,size=32m',
            '--mount', f'type=bind,source={w},target=/work', '--mount', f'type=bind,source={QEMU_USER},target=/qemu,readonly',
            '--workdir', '/work', '--entrypoint', 'python3', image, '/work/lu_entry.py']
    try:
        subprocess.run(args, capture_output=True, check=True, timeout=30)
        log(f'linux-user: {len(runs)} runs in container')
        r = subprocess.run(['docker', 'start', '--attach', name], capture_output=True, timeout=7200)
        if r.returncode: raise SystemExit('container failed: ' + r.stderr.decode()[-500:])
    finally:
        subprocess.run(['docker', 'rm', '--force', '--volumes', name], capture_output=True, timeout=60)
    cres = json.loads((w / 'results.json').read_text()); byid = {e['id']: e for e in cres['runs']}
    res = {b['name']: [] for b in benches}; insn_check = {}
    for b in benches:
        shas = {}
        for m in mults:
            n = ns[(b['name'], m)]
            ref_out = (w / 'out' / f"{b['name']}-m{m}-r0.out").read_bytes()
            got = [int(x, 16) for x in [g for g in parse_output(progs[(b['name'], m)], ref_out)['gpr']]]
            if got != models[(b['name'], n)][0]: raise SystemExit(f"FAIL linux-user {b['name']} N={n}: registers differ from the model")
            shas[m] = hashlib.sha256(ref_out).hexdigest()
        for rep in range(0, reps):
            row = []
            for m in mults:
                e = byid[f"{b['name']}-m{m}-r{rep}"]
                if e['rc'] != 0 or e['sha'] != shas[m]: raise SystemExit(f"FAIL linux-user {b['name']} m{m} rep {rep}: rc={e['rc']} output differs")
                row.append(e['t'])
            res[b['name']].append({'rep': rep, 't': row})
        dyn = cres['counts'][f"{b['name']}-c3"] - cres['counts'][f"{b['name']}-c2"]
        want = models[(b['name'], 3)][1] - models[(b['name'], 2)][1]
        insn_check[b['name']] = {'qemu_trace_delta_N3_minus_N2': dyn, 'model_delta': want, 'ok': dyn == want}
        if dyn != want: log(f"WARNING insn-count mismatch {b['name']}: QEMU trace {dyn} vs model {want}")
    return res, insn_check

def bb_pins():
    import ref; return ref.PINS
def bb_image():
    import ref; return ref.IMAGE


def stats(vals):
    med = statistics.median(vals)
    return {'median': med, 'min': min(vals), 'max': max(vals), 'range_pct': 100 * (max(vals) - min(vals)) / med,
            'cv_pct': 100 * (statistics.stdev(vals) / statistics.mean(vals) if len(vals) > 1 else 0)}

def fit(xs, ts):
    mx, mt = statistics.mean(xs), statistics.mean(ts)
    s = sum((x - mx) * (t - mt) for x, t in zip(xs, ts)) / sum((x - mx) ** 2 for x in xs)
    return s, mt - s * mx             # slope, intercept

def analyse(b, mults, ns, models, rows):
    iters = [ns[(b['name'], m)] for m in mults]; insns = [models[(b['name'], n)][1] for n in iters]
    per = []
    for r in rows:
        s_i, c = fit(insns, r['t']); s_it, _ = fit(iters, r['t'])
        pair = [(r['t'][k + 1] - r['t'][k]) / (insns[k + 1] - insns[k]) * 1e9 for k in range(len(mults) - 1)]
        per.append({'ns_per_insn': s_i * 1e9, 'ns_per_iter': s_it * 1e9, 'startup_s': c, 'pair_ns_per_insn': pair, 't': r['t']})
    return {'iters': iters, 'insns': insns, 'insns_per_iter': (insns[-1] - insns[0]) / (iters[-1] - iters[0]),
            'ns_per_insn': stats([p['ns_per_insn'] for p in per]), 'ns_per_iter': stats([p['ns_per_iter'] for p in per]),
            'startup_s': stats([p['startup_s'] for p in per]), 'reps': per}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--out', required=True, type=pathlib.Path); ap.add_argument('--reps', type=int, default=7)
    ap.add_argument('--scale', type=float, default=1.0); ap.add_argument('--bench'); ap.add_argument('--mults', default='1,2,4')
    ap.add_argument('--no-linux-user', action='store_true'); ap.add_argument('--no-system', action='store_true')
    a = ap.parse_args(); out = a.out; out.mkdir(parents=True, exist_ok=True)
    mults = [int(x) for x in a.mults.split(',')]
    benches = [b for b in bb.load()['benchmarks'] if not a.bench or b['name'] in a.bench.split(',')]
    logf = open(out / 'run.log', 'a')
    def log(s): print(s, flush=True); logf.write(s + '\n'); logf.flush()
    man = manifest(out); log(f"qemu-system {man['qemu_system']['version']} sha256 {man['qemu_system']['sha256']}")
    h0 = host_state(); log('host start: ' + json.dumps(h0))
    exe = out / 'build' / 'model'; exe.parent.mkdir(exist_ok=True); bb.build_model(exe)
    ns = build_images(out, benches, mults, a.scale)
    models = {}
    for b in benches:
        for n in [ns[(b['name'], m)] for m in mults] + [2, 3]: models[(b['name'], n)] = bb.model(b, n, exe)
    result = {'manifest': man, 'host_start': h0, 'reps': a.reps, 'mults': mults, 'scale': a.scale, 'system': {}, 'linux_user': {}, 'insn_count_check': {}}
    if not a.no_system:
        raw = run_system(out, benches, mults, ns, a.reps, models, log)
        result['system'] = {b['name']: analyse(b, mults, ns, models, raw[b['name']]) for b in benches}
    if not a.no_linux_user:
        raw, chk = run_user(out, benches, mults, ns, a.reps, models, log)
        result['linux_user'] = {b['name']: analyse(b, mults, ns, models, raw[b['name']]) for b in benches}; result['insn_count_check'] = chk
    result['host_end'] = host_state()
    result['expected'] = {f'{k[0]}-n{k[1]}': {'gpr': [f'0x{x:016x}' for x in v[0]], 'insns': v[1]} for k, v in models.items()}
    (out / 'results.json').write_text(json.dumps(result, indent=1) + '\n')
    print(f"\n{'bench':9} {'engine':11} {'insn/it':>7} {'ns/iter':>9} {'ns/insn':>8} {'range%':>7} {'cv%':>5} {'startup s':>9}")
    for b in benches:
        for eng in ('system', 'linux_user'):
            r = result[eng].get(b['name'])
            if r: print(f"{b['name']:9} {eng:11} {r['insns_per_iter']:7.3f} {r['ns_per_iter']['median']:9.3f} {r['ns_per_insn']['median']:8.4f} "
                        f"{r['ns_per_insn']['range_pct']:7.2f} {r['ns_per_insn']['cv_pct']:5.2f} {r['startup_s']['median']:9.3f}")
    if result['insn_count_check']: print('instruction-count check (linux-user -d exec vs model):', {k: v['ok'] for k, v in result['insn_count_check'].items()})

if __name__ == '__main__':
    main()
