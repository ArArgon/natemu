#!/usr/bin/env python3
"""web-tooling-profile (debug tier): the V8 team's Web Tooling Benchmark on Node.js under natemu with the profiling
resident, reporting where the time goes (docs/testing.md, "The debug tier").

    tests/bench/run_web_tooling.py run --emu natemu --kernel BZIMAGE --inputs WEB_TOOLING_INPUTS --out DIR
        --fexc FEXC --natemu NATEMU --runtime RUNTIME [--perf-log] [--sample-us N] [--vcpus 4] [--ram 4] [--cap 1500]
        [--natemu-set KEY=VALUE ...]

The guest is the Debian 6.12.107 kernel the other guest benchmarks boot and an initramfs: the input web-tooling-inputs
(tools/bench/web-tooling: Debian 13's Node.js 20 and the benchmark v0.5.3's bundle /wtb/cli.js) with this script's init
(GUEST) and a preload that prints Node's peak RSS appended (tests/bench/guest_userland.py). The init runs `node
/wtb/cli.js`: all 18 tools (acorn to uglify-js), each for the benchmark's own number of runs, then the geometric mean,
in about 7.5 minutes on natemu. The bundle is built for the browser, so the CLI's `--only` does nothing (its
process.argv is empty): every boot runs every tool.

natemu: 4 vCPUs, 4 GiB, its translation cache off; with --perf-log (the case passes it, with the `perf` resident as
--runtime) its timing log is OUT/web-tooling.perf. One boot. OUT/result.json holds each tool's runs/s and the geometric
mean as the benchmark prints them, Node's peak RSS and, from the timing log, guest_userland.perf_summary(): the resident's
buckets as shares of the vCPUs' busy time, host.translate and the state and K1 counters (shadow resets, evictions, page
faults, CR3 loads, INVLPGs). OUT/report.md says the same as text. A profile, not a timing: the runs/s are those of a
`perf` build and never go on the benchmarks page.

--sample-us N (web-tooling-sample, the `perf` and `profile` resident as --runtime; implies --perf-log): natemu's EL0
sampler every N us into OUT/NAME.samples and OUT/NAME.objects, the console's WTB-BEGIN, WTB-END and WTB-KTEXT lines
stamped on arrival in OUT/NAME.markers.tsv (as NATEMU-BENCH-START, -END, -KTEXT: the report's boot, run1 and after phases
and the kernel's _text), and the userland's ELF files as OUT/userland.cpio, for tests/bench/sample_report.py, which the
case runs around this script (its --image).
Exit 0 when every tool ran and the timing log (if asked for) has its vCPU lines; 3 when another VM ran during the
boot (contended); 1 otherwise."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'lib')); import testenv  # noqa: E401,E402 (tests/README.md)
import argparse
import re
import signal
import stat
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guest_userland as gu
from io_common import digest, write_json

# The 18 tools of v0.5.3, in the order the benchmark runs them.
TOOLS = ['acorn', 'babel', 'babel-minify', 'babylon', 'buble', 'chai', 'coffeescript', 'espree', 'esprima', 'jshint',
         'lebab', 'postcss', 'prepack', 'prettier', 'source-map', 'terser', 'typescript', 'uglify-js']
INIT = 'wtb-init'
CMDLINE = f'console=ttyS0 mitigations=off nokaslr rdinit=/{INIT}'
GUEST = r'''#!/bin/sh
# wtb-init: the Web Tooling Benchmark on Node.js, then power off. Markers "WTB..." on the console.
B=/bin/busybox
$B mount -t proc proc /proc; $B mount -t sysfs sys /sys; $B mount -t devtmpfs dev /dev; $B mount -t tmpfs tmp /tmp
export HOME=/root PATH=/usr/sbin:/usr/bin:/sbin:/bin
echo "WTB-KTEXT $($B grep ' _text$' /proc/kallsyms | $B cut -d' ' -f1)"
echo "WTB-BEGIN cpus=$(nproc) node=$(node --version) $(grep MemTotal /proc/meminfo | tr -s ' ')"
{ node -r /wtb/rss.js /wtb/cli.js; echo "rc=$?"; } 2>&1 | $B sed 's/^/WTB /'
echo "WTB-END uptime=$(cut -d' ' -f1 /proc/uptime)"
$B sync; $B poweroff -f
'''
# The console lines a sampled boot's markers stamp (guest_userland.Stamper): the report's phases and the kernel's _text.
MARKS = [(r'^WTB-KTEXT ([0-9a-f]+)', r'NATEMU-BENCH-KTEXT \1'), (r'^WTB-BEGIN ', 'NATEMU-BENCH-START'), (r'^WTB-END ', 'NATEMU-BENCH-END')]
RSS = b'// Preloaded with node -r: the peak resident set at exit.\nprocess.on("exit", () => console.log(`WTB-MAXRSS ${Math.round(process.resourceUsage().maxRSS / 1024)} MiB`));\n'


def tail() -> bytes:
    return gu.tail_cpio([(INIT, stat.S_IFREG | 0o755, GUEST.encode()), ('wtb/rss.js', stat.S_IFREG | 0o644, RSS)])


def parse(serial: str) -> dict:
    """The boot's results from its console: each tool's runs/s, the geometric mean, Node's peak RSS, the exit status."""
    tools = {m.group(1): float(m.group(2)) for m in re.finditer(r'^WTB +([\w-]+): +([0-9.]+) runs/s', serial, re.M)}
    gm = re.search(r'^WTB Geometric mean: +([0-9.]+) runs/s', serial, re.M)
    rss = re.search(r'^WTB WTB-MAXRSS (\d+) MiB', serial, re.M)
    rc = re.search(r'^WTB rc=(\d+)', serial, re.M)
    begin = re.search(r'^WTB-BEGIN cpus=(\d+) node=(\S+)', serial, re.M)
    end = re.search(r'^WTB-END uptime=([0-9.]+)', serial, re.M)
    return {'tools': tools, 'geomean': float(gm.group(1)) if gm else None, 'max_rss_mib': int(rss.group(1)) if rss else None,
            'rc': int(rc.group(1)) if rc else None, 'cpus': int(begin.group(1)) if begin else None,
            'node': begin.group(2) if begin else None, 'uptime': float(end.group(1)) if end else None}


def problems(r: dict, vcpus: int) -> list:
    out = []
    if r['uptime'] is None: out.append('no WTB-END line')
    missing = [t for t in TOOLS if t not in r['tools']]
    if missing: out.append(f'tools missing: {", ".join(missing)}')
    if extra := sorted(set(r['tools']) - set(TOOLS)): out.append(f'unknown tools: {", ".join(extra)}')
    if r['geomean'] is None: out.append('no geometric mean')
    if r['rc'] != 0: out.append(f'node exit status {r["rc"]}')
    if r['cpus'] != vcpus: out.append(f'guest saw {r["cpus"]} CPUs, not {vcpus}')
    return out


def report(result: dict) -> str:
    lines = [f'# {result["name"]}', '', f'natemu {result["natemu"][:16]}, runtime {result["runtime"][:16]}, '
             f'{result["vcpus"]} vCPUs, {result["ram_gib"]} GiB, boot {result["seconds"]} s', '',
             '| tool | runs/s |', '| --- | ---: |']
    lines += [f'| {t} | {result["tools"].get(t)} |' for t in TOOLS]
    lines += [f'| **geometric mean** | **{result["geomean"]}** |', '', f'Node peak RSS {result["max_rss_mib"]} MiB.', '',
              '```', *gu.perf_lines(result.get('perf')), '```', '']
    return '\n'.join(lines)


def run(args) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    initrd = args.out / 'initrd.img'
    made = gu.make_initrd(args.inputs, initrd, tail())
    perf = args.out / f'{args.name}.perf' if args.perf_log or args.sample_us else None
    sample = (args.out / args.name, args.sample_us) if args.sample_us else None
    stamper = gu.Stamper(args.out / f'{args.name}.markers.tsv', MARKS) if sample else None
    if sample: gu.elf_image(args.inputs / 'userland.cpio.zst', args.out / 'userland.cpio')
    try:
        b = gu.boot_local(args.out, 1, gu.natemu_command(args, initrd, CMDLINE, args.out / 'boot-1.serial', perf, sample=sample),
                          args.cap, stamper)
    finally:
        initrd.unlink(missing_ok=True)
    r = parse(b.pop('text'))
    bad = problems(r, args.vcpus) + (['capped'] if b['capped'] else [])
    summary = gu.perf_summary(perf.read_text(errors='replace')) if perf and perf.exists() else None
    if perf and not summary: bad.append('the timing log has no vcpu lines')
    result = {'name': args.name, 'emu': 'natemu', 'vcpus': args.vcpus, 'ram_gib': args.ram, 'inputs': made,
              'kernel': digest(args.kernel), 'natemu': digest(args.natemu), 'runtime': digest(args.runtime),
              **r, **b, 'problems': bad, 'pass': not bad, 'perf': summary}
    write_json(args.out / 'result.json', result)
    if not bad: (args.out / 'report.md').write_text(report(result))
    print(f'boot: {b["seconds"]} s, geometric mean {r["geomean"]} runs/s, Node peak RSS {r["max_rss_mib"]} MiB' +
          (f'; problems: {"; ".join(bad)}' if bad else '') + (f'; contended by {b["contended"]}' if b['contended'] else ''))
    for t in TOOLS: print(f'  {t:13} {r["tools"].get(t)} runs/s')
    for line in gu.perf_lines(summary) if perf else []: print(line)
    print('PASS' if not bad else 'FAIL', args.out, flush=True)
    if bad: return 1
    return 3 if b['contended'] else 0


def main():
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = a.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--emu', choices=('natemu',), required=True)
    r.add_argument('--name', default='web-tooling')
    r.add_argument('--kernel', type=Path, required=True)
    r.add_argument('--inputs', type=Path, required=True, help='the web-tooling-inputs folder (testkit/inputs.py:web_tooling_inputs)')
    r.add_argument('--out', type=Path, required=True)
    r.add_argument('--perf-log', action='store_true', help="natemu's timing log, OUT/NAME.perf, summarized (pass a `perf` resident as --runtime)")
    r.add_argument('--sample-us', type=int, help="natemu's EL0 sampler every N us (pass a `perf,profile` resident as --runtime; implies --perf-log)")
    r.add_argument('--vcpus', type=int, default=4); r.add_argument('--ram', type=int, default=4, help='GiB')
    r.add_argument('--cap', type=float, default=1500, help='seconds for the boot')
    r.add_argument('--fexc', type=Path, required=True); r.add_argument('--natemu', type=Path, required=True)
    r.add_argument('--runtime', type=Path, required=True)
    r.add_argument('--natemu-set', action='append', default=[], metavar='KEY=VALUE', help='passed on as --set KEY=VALUE (the bench harness turns the platform devices off with it)')
    args = a.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    return run(args)


if __name__ == '__main__':
    testenv.require_framework(__file__)
    sys.exit(main())
