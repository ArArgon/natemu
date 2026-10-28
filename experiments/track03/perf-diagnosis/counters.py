#!/usr/bin/env python3
"""PMU counts per loop iteration (Apple kperf, root) for the stride-4096 question: the same memory loop natively on the host,
in the probe VM (4 KiB and 16 KiB stage-1 pages), under natemu and under QEMU system TCG; plus the two hand-written chases.

    python3 counters.py prepare OUT          # as the normal user: builds binaries, images, natemu programs, QEMU images
    python3 counters.py run OUT --dry        # as the normal user: runs every command once without counters (smoke test)
    sudo python3 counters.py run OUT         # the measurement (about 3-5 minutes); writes OUT/counters.json, prints a table

Per subject and point the command runs at N and 2N, REPS times, under kpcrun (system-wide counters, all CPUs, all ELs);
per-iteration count = (count(2N) - count(N)) / (iterations(2N) - iterations(N)), median over REPS. See README.md."""
import json, os, pathlib, statistics, subprocess, sys
HERE = pathlib.Path(__file__).resolve().parent; ROOT = HERE.parents[2]
EVENTS = ('L1D_CACHE_MISS_LD,L1D_CACHE_MISS_ST,L1D_TLB_MISS,ST_MEM_ORDER_VIOL_LD_NONSPEC,'
          'L2_TLB_MISS_DATA,MMU_TABLE_WALK_DATA,L1D_TLB_ACCESS,LD_UNIT_WAITING_YOUNG_L1D_CACHE_MISS')
N, REPS = 20_000_000, 3
POINTS = [(4096, 0xfffff), (4160, 0xfffff), (2048, 0xfffff), (4096, 0x3ffff), (4096, 0x7ffff)]   # stride, wrap mask
CHASES = [('load', 4096, 0xfffff), ('loadstore', 4096, 0xfffff), ('load', 4160, 0xfffff)]

def prepare(out):
    os.environ.setdefault('NATEMU_RAW_ROOT', str(ROOT.parent / 'natemu-raw'))
    sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT / 'tools')); sys.path.insert(0, str(ROOT / 'tools/bench'))
    import run as rn, run_slice1_vm as vm, build_bench as bb, run_qemu as rq
    rn.build(out); b_ = out / 'bin'; (out / 'qemu').mkdir(exist_ok=True)
    subprocess.run(['clang', '-O2', '-Wall', '-Werror', '-o', str(b_ / 'kpcrun'), str(HERE / 'kpcrun.c')], check=True)
    host, runtime = vm.build(); fexc = os.environ['FEXC']
    m4096 = [x for x in bb.load()['benchmarks'] if x['name'] == 'mem4096'][0]
    jobs = []   # {subject, point, iters_per_n, cmd(N), cmd(2N), expect}
    def vmloop_jobs(tag, cfg_for):
        for subj, args in (('host', ('1', 'host', '1')), ('vm_4k', ('1', '4k', '1')), ('vm_16k', ('1', '16k', '1'))):
            # vmloop REPS=1 runs a warm-up and one timed round, each N and 2N: 6 x n iterations per invocation
            jobs.append({'subject': subj, 'point': tag, 'iters': [6 * N, 12 * N], 'cmd': [[str(b_ / 'vmloop'), str(cfg_for(k)), *args] for k in (N, 2 * N)], 'expect': 0})
    for stride, mask in POINTS:
        tag = f'loop s{stride} {(mask + 1) // 4096}p'
        cfgs = {k: rn.image(out, f'ctr-{stride}-{mask:x}-{k}', 'mem4096', 'base', k, stride, mask)[0] for k in (N, 2 * N)}
        vmloop_jobs(tag, cfgs.get)
        c = bytearray.fromhex(m4096['code'])
        i = c.index(bytes.fromhex('4881c6')); c[i + 3:i + 7] = stride.to_bytes(4, 'little')
        j = c.index(bytes.fromhex('4881e6')); c[j + 3:j + 7] = mask.to_bytes(4, 'little')
        cmds = []
        for k in (N, 2 * N):
            w = out / 'natemu' / f'{stride}-{mask:x}-{k}'; w.mkdir(parents=True, exist_ok=True)
            p = {'name': w.name, 'code': c.hex(), 'rip': m4096['rip'], 'rflags': 0,
                 'gpr': [f'0x{g:x}' for g in [0, k, 0, 0x200000, 0x301800, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]],
                 'memory': [{'addr': 0x300000, 'data': '00' * 8192}, {'addr': 0x200000, 'data': '00' * (mask + 1)}], 'insns': m4096['insns']}
            (w / 'program.txt').write_text(vm.description(p))
            cmds.append([str(host), str(runtime), 'run-program', fexc, str(w / 'program.txt'), str(w), '--watchdog-ms', '600000'])
        jobs.append({'subject': 'natemu', 'point': tag, 'iters': [N, 2 * N], 'cmd': cmds, 'expect': 0, 'expect_out': 'reached'})
        b = dict(m4096, name=f'q{stride}-{mask:x}', code=c.hex(), n_default=N)
        ns = rq.build_images(out / 'qemu', [b], [1, 2], 1.0)
        jobs.append({'subject': 'qemu', 'point': tag, 'iters': [N, 2 * N], 'expect': 1,
                     'cmd': [[rq.QEMU_SYS, *rq.SYS_ARGS, '-debugcon', f'file:{out}/qemu/dbg.out', '-kernel', str(out / 'qemu' / 'images' / f"{b['name']}-n{ns[(b['name'], m)]}.bin")] for m in (1, 2)]})
    for kind, stride, mask in CHASES:
        img = out / 'img' / f'chase-{kind}.bin'; img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(__import__('struct').pack(f'<{len(rn.KINDS[kind])}I', *rn.KINDS[kind]) + bytes(64))
        def cfg_for(k, kind=kind, stride=stride, mask=mask):
            p = out / 'img' / f'ctr-chase-{kind}-{stride}-{mask:x}-{k}.cfg'
            p.write_text(f'image {img}\nentry 4\nn {k}\nctr 1\nreg 6 buf+0\nfill chain:{stride}:{mask:x}\nbufbytes {max(mask + 1, 1 << 20)}\n')
            return p
        vmloop_jobs(f'chase-{kind} s{stride} {(mask + 1) // 4096}p', cfg_for)
    (out / 'manifest.json').write_text(json.dumps({'events': EVENTS, 'n': N, 'reps': REPS, 'jobs': jobs}, indent=1) + '\n')
    print(f'{len(jobs)} jobs written to {out / "manifest.json"}')

def run(out, dry):
    m = json.loads((out / 'manifest.json').read_text()); kpc = out / 'bin' / 'kpcrun'; res = []
    load = subprocess.run(['uptime'], capture_output=True, text=True).stdout.strip()
    for job in m['jobs']:
        per = {}
        for rep in range(1 if dry else m['reps']):
            counts = []
            for cmd in job['cmd']:
                if dry:
                    r = subprocess.run(cmd, capture_output=True, text=True); counts.append({'rc': r.returncode, 'events': {}})
                else:
                    r = subprocess.run([str(kpc), m['events'], '--', *cmd], capture_output=True, text=True)
                    if r.returncode: sys.exit(f'kpcrun failed: {r.stderr.strip()}')
                    counts.append(json.loads(r.stdout))
                text = r.stdout if dry else counts[-1]['child_stdout']
                if job.get('expect_out', '') not in text: sys.exit(f"{job['subject']} {job['point']}: output lacks {job['expect_out']!r}: {text[-300:]}")
                if counts[-1]['rc'] != job['expect']: sys.exit(f"{job['subject']} {job['point']}: exit {counts[-1]['rc']} (expected {job['expect']}): {cmd}")
            di = job['iters'][1] - job['iters'][0]
            for e in counts[0]['events']: per.setdefault(e, []).append((counts[1]['events'][e] - counts[0]['events'][e]) / di)
            if not dry: per.setdefault('ns', []).append(1e9 * (counts[1]['wall_s'] - counts[0]['wall_s']) / di)
        row = {'subject': job['subject'], 'point': job['point'], **{e: statistics.median(v) for e, v in per.items()}}
        res.append(row); print(json.dumps(row), flush=True)
    (out / ('counters-dry.json' if dry else 'counters.json')).write_text(json.dumps({'uptime_start': load, 'rows': res,
        'uptime_end': subprocess.run(['uptime'], capture_output=True, text=True).stdout.strip()}, indent=1) + '\n')
    if not dry:
        cols = ['ns', 'FIXED_CYCLES', 'FIXED_INSTRUCTIONS'] + m['events'].split(',')
        print('subject  point  ' + '  '.join(c.replace('L1D_', '').replace('_DATA', '')[:14] for c in cols))
        for r in res: print(f"{r['subject']:7} {r['point']:24} " + ' '.join(f'{r.get(c, float("nan")):10.3f}' for c in cols))

if __name__ == '__main__':
    out = pathlib.Path(sys.argv[2]).resolve()
    if sys.argv[1] == 'prepare': out.mkdir(parents=True, exist_ok=True); prepare(out)
    else: run(out, '--dry' in sys.argv)
