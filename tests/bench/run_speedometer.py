#!/usr/bin/env python3
"""speedometer-*: WebKit's Speedometer 3.1 in headless Firefox ESR, booted under natemu, QEMU or, for the real-x86
reference, KVM on the x86 machine (docs/benchmarks.md, "Web browsing"); speedometer-profile, its debug-tier profile.

    tests/bench/run_speedometer.py run --emu natemu|qemu|kvm --kernel BZIMAGE --inputs SPEEDOMETER_INPUTS --out DIR
        [--boots 3] [--iterations 5] [--vcpus 4] [--ram 4] [--cap 900]
        [--fexc FEXC --natemu NATEMU --runtime RUNTIME [--perf-log] [--compile-cache off|warm [--compile-cache-mb 8192]]
         [--natemu-set KEY=VALUE ...]]                                                                 (natemu)
        [--qemu QEMU_SYSTEM]                                                                           (qemu)

The guest is the Debian 6.12.107 kernel the other guest benchmarks boot and an initramfs: the input speedometer-inputs
(tools/bench/speedometer: Debian 13's firefox-esr 140, Python 3 and Speedometer at release/3.1) with a second cpio
appended here (tests/bench/guest_userland.py): this script's init (GUEST) and the files of tests/bench/speedometer: the
results hook (hook.js), the local web server (serve.py), Firefox's profile (user.js) and policies (policies.json). The
init puts the hook into Speedometer's index.html before its own script, starts the server on 127.0.0.1:8080 and
Firefox headless with a fresh copy of the profile at `index.html?startAutomatically&iterationCount=N`. Speedometer runs
its 20 suites N times; the hook POSTs the results to the server, which prints them on the console ("SP /done {...}")
and exits, and the init powers off. Nothing leaves the guest: it has only the loopback network.

The numbers are Speedometer's own: per boot its score (the mean of the iterations' scores, higher is better), each
iteration's score and each suite's mean time in ms. Each of BOOTS boots is one line of OUT/bench.jsonl; OUT/result.json
holds the median of the boots' scores with their spread (min, max) and each suite's median of the boots' means. 3 boots
of 5 iterations keep a case under about 10 minutes on an M4 (natemu about 140 s a boot, QEMU about 200 s).

natemu: 4 vCPUs, 4 GiB, its translation cache off (as the other guest benchmarks): every boot translates Firefox from
nothing. --compile-cache warm (speedometer-natemu-warm, speedometer-profile-warm) is how a desktop runs it instead, its
translated code kept on disk from earlier sessions: natemu's persistent cache is a folder in OUT (compile-cache, its cap
--compile-cache-mb, large enough that nothing is evicted), filled by one untimed boot of the same iterations (boot 0,
recorded in bench.jsonl as the fill, never in the summary; it must pass too), then the BOOTS timed boots reuse it (and
add to it, as later sessions would). The folder's packs and bytes after each boot are recorded; it is removed at the
end. With --perf-log (the debug case,
with the `perf` resident as --runtime) each boot writes its timing log, OUT/boot-I.perf, summarized as the Web Tooling
profile is (guest_userland.perf_summary: buckets as shares of cpu_ns, host.translate, CR3 loads and the TLB bucket's
cost, shadow resets, evictions, page faults, INVLPGs): a browser is many processes per vCPU, so CR3 loads matter here.
QEMU: the pinned qemu-system-x86_64, TCG thread=multi, the other guest benchmarks' board and CPU model. kvm: the x86
machine NATEMU_X86_HOST boots the same kernel and initramfs under KVM, the same CPU model (guest_userland.boots_kvm).
Exit 0 when every boot finished every iteration; 3 when another VM ran during a boot (contended: not comparable); 1
otherwise."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import json
import re
import shutil
import signal
import stat
import statistics
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import guest_userland as gu
from io_common import digest, write_json

# Speedometer 3.1's 20 suites, in its order.
SUITES = ['TodoMVC-JavaScript-ES5', 'TodoMVC-JavaScript-ES6-Webpack-Complex-DOM', 'TodoMVC-WebComponents',
          'TodoMVC-React-Complex-DOM', 'TodoMVC-React-Redux', 'TodoMVC-Backbone', 'TodoMVC-Angular-Complex-DOM',
          'TodoMVC-Vue', 'TodoMVC-jQuery', 'TodoMVC-Preact-Complex-DOM', 'TodoMVC-Svelte-Complex-DOM',
          'TodoMVC-Lit-Complex-DOM', 'NewsSite-Next', 'NewsSite-Nuxt', 'Editor-CodeMirror', 'Editor-TipTap',
          'Charts-observable-plot', 'Charts-chartjs', 'React-Stockcharts-SVG', 'Perf-Dashboard']
HARNESS = HERE / 'speedometer'           # hook.js, serve.py, user.js, policies.json
INIT = 'sp-init'
CMDLINE = f'console=ttyS0 mitigations=off nokaslr rdinit=/{INIT}'
GUEST = r'''#!/bin/sh
# sp-init: Speedometer in headless Firefox, its results printed by serve.py, then power off. Markers "SP..." on the console.
B=/bin/busybox
$B mount -t proc proc /proc; $B mount -t sysfs sys /sys; $B mount -t devtmpfs dev /dev; $B mount -t tmpfs tmp /tmp
$B mkdir -p /dev/shm /dev/pts && $B mount -t tmpfs shm /dev/shm && $B mount -t devpts pts /dev/pts
$B ifconfig lo 127.0.0.1 up
export HOME=/root PATH=/usr/sbin:/usr/bin:/sbin:/bin MOZ_HEADLESS=1 MOZ_CRASHREPORTER_DISABLE=1
iter=$($B sed -n 's/.*spiter=\([0-9]*\).*/\1/p' /proc/cmdline)
$B sed -i 's|<script src="resources/main.mjs" type="module"></script>|<script src="hook.js"></script>&|' /sp/index.html
$B grep -q '<script src="hook.js"></script><script src="resources/main.mjs"' /sp/index.html || echo "SP-NO-HOOK"
echo "SP-BEGIN cpus=$(nproc) iter=$iter $(grep MemTotal /proc/meminfo | tr -s ' ')"
$B mkdir -p /tmp/prof && $B cp /sp-harness/user.js /tmp/prof/user.js
python3 /sp-harness/serve.py & SRV=$!
$B sleep 1
firefox-esr --headless --no-remote --profile /tmp/prof --window-size 1366,768 \
  "http://127.0.0.1:8080/index.html?startAutomatically&iterationCount=$iter" >/tmp/ff.log 2>&1 &
FF=$!
( n=0; while $B kill -0 $SRV 2>/dev/null && $B kill -0 $FF 2>/dev/null; do $B sleep 5; n=$((n + 1))
    [ $((n % 12)) = 0 ] && echo "SP-MEM $(cut -d' ' -f1 /proc/uptime) $(grep MemAvailable /proc/meminfo | tr -s ' ')"; done
  $B kill -0 $FF 2>/dev/null || { echo "SP-FF-EXITED"; $B kill $SRV; } ) &
wait $SRV
echo "SP-END uptime=$(cut -d' ' -f1 /proc/uptime)"
$B sed 's/^/SP-FF /' /tmp/ff.log | $B tail -40
$B kill $FF 2>/dev/null; $B sync; $B poweroff -f
'''


def tail() -> bytes:
    f = lambda name: (HARNESS / name).read_bytes()
    return gu.tail_cpio([(INIT, stat.S_IFREG | 0o755, GUEST.encode()), *gu.dirs('sp-harness'),
                         ('sp/hook.js', stat.S_IFREG | 0o644, f('hook.js')),
                         ('sp-harness/serve.py', stat.S_IFREG | 0o644, f('serve.py')),
                         ('sp-harness/user.js', stat.S_IFREG | 0o644, f('user.js')),
                         *gu.dirs('usr/lib/firefox-esr/distribution'),
                         ('usr/lib/firefox-esr/distribution/policies.json', stat.S_IFREG | 0o644, f('policies.json'))])


def parse(serial: str) -> dict:
    """One boot's results from its console: Speedometer's (the hook's /done), the page's errors, progress, the guest."""
    done = re.search(r'^SP /done (\{.*\})\s*$', serial, re.M)
    try: res = json.loads(done.group(1)) if done else None
    except ValueError: res = {'error': 'unreadable /done line'}
    errors = []
    for m in re.finditer(r'^SP /log (\{.*\})\s*$', serial, re.M):
        try: d = json.loads(m.group(1))
        except ValueError: continue
        if 'error' in d: errors.append(d)
    begin = re.search(r'^SP-BEGIN cpus=(\d+) iter=(\d*)', serial, re.M)
    suites = (res or {}).get('suites') or {}
    return {'score': (res or {}).get('score'), 'score_text': (res or {}).get('scoreText'),
            'per_iteration': (res or {}).get('perIteration'), 'geomean_ms': (res or {}).get('geomeanMs'),
            'suites': {n: suites[n]['mean'] for n in SUITES if n in suites}, 'suite_values': {n: s.get('values') for n, s in suites.items()},
            'unknown_suites': sorted(set(suites) - set(SUITES)), 'error': (res or {}).get('error'), 'done': res is not None,
            'ua': (res or {}).get('ua'), 'page_errors': errors, 'hook': 'SP-NO-HOOK' not in serial,
            'firefox_exited': 'SP-FF-EXITED' in serial, 'cpus': int(begin.group(1)) if begin else None,
            'iterations': int(begin.group(2)) if begin and begin.group(2) else None,
            'ended': re.search(r'^SP-END ', serial, re.M) is not None}


def problems(r: dict, iterations: int, vcpus: int) -> list:
    """Why a boot's results do not count (empty: they do)."""
    out = []
    if not r['hook']: out.append("the hook is not in Speedometer's index.html")
    if r['firefox_exited']: out.append('Firefox exited before the results')
    if not r['done']: out.append('no results (no /done)')
    if r['error']: out.append(f'Speedometer failed: {r["error"][:300]}')
    if not r['ended']: out.append('no SP-END line')
    if r['cpus'] != vcpus: out.append(f'guest saw {r["cpus"]} CPUs, not {vcpus}')
    if r['iterations'] != iterations: out.append(f'the guest asked for {r["iterations"]} iterations, not {iterations}')
    if r['done'] and not r['error']:
        if not isinstance(r['score'], (int, float)): out.append('no score')
        if len(r['per_iteration'] or []) != iterations: out.append(f'{len(r["per_iteration"] or [])} iterations measured, not {iterations}')
        if missing := [n for n in SUITES if n not in r['suites']]: out.append(f'suites missing: {", ".join(missing)}')
        if r['unknown_suites']: out.append(f'unknown suites: {", ".join(r["unknown_suites"])}')
        if 'Firefox/' not in (r['ua'] or ''): out.append(f'user agent {r["ua"]!r}: not Firefox')
    return out


def summary(boots: list) -> dict:
    """The median and spread of the boots' scores and geomeans, each suite's median of the boots' means."""
    def stats(xs): return {'median': statistics.median(xs), 'min': min(xs), 'max': max(xs), 'all': xs}
    return {'score': stats([b['score'] for b in boots]), 'geomean_ms': stats([b['geomean_ms'] for b in boots]),
            'score_text': [b['score_text'] for b in boots], 'suites': {n: stats([b['suites'][n] for b in boots]) for n in SUITES}}


def run(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    initrd = args.out / 'initrd.img'
    made = gu.make_initrd(args.inputs, initrd, tail())
    cmdline = f'{CMDLINE} spiter={args.iterations}'
    (args.out / 'bench.jsonl').unlink(missing_ok=True)
    boots, filled = [], None
    cache = args.out / 'compile-cache' if args.compile_cache == 'warm' else 'off'
    if cache != 'off' and cache.exists(): shutil.rmtree(cache)
    first = 0 if cache != 'off' else 1
    try:
        remote = gu.boots_kvm(args, initrd, [cmdline] * args.boots, 'speedometer') if args.emu == 'kvm' else None
        for i in range(first, args.boots + 1):
            fill = i == 0
            serial = args.out / f'boot-{i}.serial'
            perf = args.out / f'boot-{i}.perf' if args.perf_log and not fill else None
            if remote: b = remote[i - 1]
            elif args.emu == 'natemu':
                b = gu.boot_local(args.out, i, gu.natemu_command(args, initrd, cmdline, serial, perf, cache, args.compile_cache_mb), args.cap)
            else: b = gu.boot_local(args.out, i, gu.qemu_command(args, initrd, cmdline, serial), args.cap)
            r = parse(b.pop('text'))
            r.update(boot=i, fill=fill, **b)
            if cache != 'off': r['compile_cache'] = gu.cache_size(cache)
            r['problems'] = problems(r, args.iterations, args.vcpus) + (['capped'] if r['capped'] else [])
            if perf:
                r['perf'] = gu.perf_summary(perf.read_text(errors='replace')) if perf.exists() else None
                if not r['perf']: r['problems'].append('the timing log has no vcpu lines')
            if fill: filled = r
            else: boots.append(r)
            with open(args.out / 'bench.jsonl', 'a') as f: f.write(json.dumps({'name': args.name, 'emu': args.emu, **r}) + '\n')
            print(f'boot {i}{" (fills the compile cache, untimed)" if fill else ""}: {r["seconds"]} s, score {r["score_text"]}' +
                  (f', compile cache {r["compile_cache"]["packs"]} packs, {r["compile_cache"]["bytes"] / 2**20:.0f} MiB' if cache != 'off' else '') +
                  (f'; problems: {"; ".join(r["problems"])}' if r['problems'] else '') +
                  (f'; page errors: {r["page_errors"][:3]}' if r['page_errors'] else '') +
                  (f'; contended by {r["contended"]}' if r['contended'] else ''), flush=True)
            for line in gu.perf_lines(r['perf']) if perf else []: print('  ' + line)
            if r['problems']: break
    finally:
        initrd.unlink(missing_ok=True)
        if cache != 'off': shutil.rmtree(cache, ignore_errors=True)
    ok = len(boots) == args.boots and not any(b['problems'] for b in boots) and not (filled and filled['problems'])
    result = {'name': args.name, 'emu': args.emu, 'vcpus': args.vcpus, 'ram_gib': args.ram, 'boots': len(boots),
              'iterations': args.iterations, 'inputs': made, 'kernel': digest(args.kernel), 'pass': ok,
              'ua': boots[0]['ua'] if boots else None, 'perf_log': args.perf_log, 'compile_cache': args.compile_cache}
    if filled: result['fill'] = {k: filled[k] for k in ('score', 'score_text', 'seconds', 'compile_cache', 'problems')}
    if args.emu == 'natemu': result.update(natemu=digest(args.natemu), runtime=digest(args.runtime))
    if args.emu == 'qemu': result['qemu'] = subprocess.run([str(args.qemu), '--version'], capture_output=True, text=True).stdout.splitlines()[0]
    if args.emu == 'kvm' and boots: result['reference'] = boots[0].get('reference')
    if ok: result.update(summary(boots))
    if ok and args.perf_log: result['perf'] = [b['perf'] for b in boots]
    write_json(args.out / 'result.json', result)
    if ok:
        s = result['score']
        print(f'median over {len(boots)} boots of {args.iterations} iterations: score {s["median"]:.3f} ({s["min"]:.3f}-{s["max"]:.3f}); '
              f'Speedometer per boot: {", ".join(result["score_text"])}')
        for n in SUITES:
            m = result['suites'][n]
            print(f'  {n:44} {m["median"]:8.1f} ms ({m["min"]:.1f}-{m["max"]:.1f})')
    print('PASS' if ok else 'FAIL', args.out, flush=True)
    if not ok: return 1
    return 3 if any(b['contended'] for b in boots) else 0


def main():
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = a.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--emu', choices=('natemu', 'qemu', 'kvm'), required=True)
    r.add_argument('--name', default='speedometer')
    r.add_argument('--kernel', type=Path, required=True)
    r.add_argument('--inputs', type=Path, required=True, help='the speedometer-inputs folder (testkit/inputs.py:speedometer_inputs)')
    r.add_argument('--out', type=Path, required=True)
    r.add_argument('--boots', type=lambda v: max(1, int(v)), default=3, help='boots (the median score is reported)')
    r.add_argument('--iterations', type=lambda v: max(1, int(v)), default=5, help="Speedometer's iterationCount per boot")
    r.add_argument('--vcpus', type=int, default=4); r.add_argument('--ram', type=int, default=4, help='GiB')
    r.add_argument('--cap', type=float, default=900, help='seconds per boot')
    r.add_argument('--fexc', type=Path); r.add_argument('--natemu', type=Path); r.add_argument('--runtime', type=Path)
    r.add_argument('--perf-log', action='store_true', help="natemu: each boot's timing log, OUT/boot-I.perf, summarized (pass a `perf` resident as --runtime)")
    r.add_argument('--compile-cache', choices=('off', 'warm'), default='off',
                   help="natemu: off (every boot translates from nothing) or warm (an untimed boot fills a persistent cache in OUT that the timed boots reuse)")
    r.add_argument('--compile-cache-mb', type=int, default=8192, help='natemu, warm: the persistent cache\'s cap in MiB')
    r.add_argument('--natemu-set', action='append', default=[], metavar='KEY=VALUE', help='natemu: passed on as --set KEY=VALUE (the bench harness turns the platform devices off with it)')
    r.add_argument('--qemu', type=Path)
    args = a.parse_args()
    if args.emu == 'natemu' and not (args.fexc and args.natemu and args.runtime): a.error('--emu natemu needs --fexc, --natemu and --runtime')
    if args.emu == 'qemu' and not args.qemu: a.error('--emu qemu needs --qemu')
    if args.perf_log and args.emu != 'natemu': a.error('--perf-log is natemu only')
    if args.compile_cache != 'off' and args.emu != 'natemu': a.error('--compile-cache warm is natemu only')
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    return run(args)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main())
